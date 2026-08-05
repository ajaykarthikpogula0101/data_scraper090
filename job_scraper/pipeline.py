import csv
import logging
import os
import threading
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import openpyxl

from .config import (
    INPUT_FILE,
    OUTPUT_FILE,
    OUTPUT_COLUMNS,
    DEFAULT_WORKERS,
    LOG_FILE,
)
from .company import process_company_details
from .session import ScrapeSession
from .fields import now_iso, extract_labeled_fields, experience_year_range
from .clean_html import html_to_plain_text
from .detect_language import detect_language
from .urlutils import normalize_website, hostname

log = logging.getLogger("job_scraper")
_RETRYABLE_STATUSES = {"Unreachable", "Career Page Not Found",
                       "Career Page Found - Extraction Unsupported", "Error"}


def read_companies(input_file):
    wb = openpyxl.load_workbook(input_file, read_only=True)
    ws = wb.active
    rows = []
    for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True)):
        if row[0] is None or not str(row[0]).strip():
            continue
        name = str(row[0]).strip()
        web = str(row[1]).strip() if row[1] else ""
        country = str(row[2]).strip() if row[2] else ""
        rows.append((name, web, country))
    wb.close()
    return rows


def load_completed(output_file):
    done = set()
    if not os.path.exists(output_file):
        return done
    with open(output_file, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = "%s|%s" % (row.get("company_name", ""), row.get("website", ""))
            done.add(key)
    return done


def remove_retryable_rows(output_file):
    if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
        return 0
    with open(output_file, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    kept = [row for row in rows if row.get("job_status") not in _RETRYABLE_STATUSES]
    removed = len(rows) - len(kept)
    if not removed:
        return 0
    directory = os.path.dirname(os.path.abspath(output_file))
    fd, temporary = tempfile.mkstemp(prefix=".retry_", suffix=".csv", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(kept)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_file)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return removed


class CsvWriter:
    def __init__(self, path, columns):
        self.path = path
        self.columns = columns
        self.lock = threading.Lock()
        self._ensure_header()

    def _ensure_header(self):
        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            with open(self.path, "r", encoding="utf-8-sig", newline="") as f:
                if f.read(2000).lstrip("\ufeff").startswith("company_name"):
                    return
        with open(self.path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(self.columns)

    def write_rows(self, rows):
        if not rows:
            return
        with self.lock:
            with open(self.path, "a", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=self.columns, extrasaction="ignore")
                for r in rows:
                    w.writerow(r)


def _company_key(row):
    return "%s|%s" % (row[0], row[1])


def run(
    input_file=INPUT_FILE,
    output_file=OUTPUT_FILE,
    limit=0,
    offset=0,
    workers=DEFAULT_WORKERS,
    resume=True,
    countries=None,
    quiet=True,
    enable_search=True,
    retry_failures=False,
):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
    )
    if quiet:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)

    companies = read_companies(input_file)
    log.info("Total companies in input: %d", len(companies))

    if countries:
        companies = [c for c in companies if c[2] in countries]
        log.info("After country filter: %d", len(companies))

    if offset:
        companies = companies[offset:]
    if limit:
        companies = companies[:limit]
    log.info("Companies to process this run: %d", len(companies))

    if retry_failures:
        removed = remove_retryable_rows(output_file)
        log.info("Removed %d retry-eligible rows before reprocessing", removed)
    writer = CsvWriter(output_file, OUTPUT_COLUMNS)
    completed = load_completed(output_file) if resume else set()
    todo = [c for c in companies if _company_key(c) not in completed]
    log.info("Skipping %d already processed; %d to do", len(companies) - len(todo), len(todo))

    counters = {"processed": 0, "ok": 0, "no_jobs": 0, "unsupported": 0,
                "career_not_found": 0, "unreachable": 0, "error": 0, "jobs": 0}
    counters_lock = threading.Lock()
    session_factory = lambda: ScrapeSession()
    domain_results = {}
    domain_inflight = {}
    domain_cache_lock = threading.Lock()

    def process_with_domain_cache(row, session):
        cache_key = hostname(normalize_website(row[1])) if row[1] else ""
        if not cache_key:
            return process_company_details(row, session, enable_search=enable_search)
        owner = False
        with domain_cache_lock:
            if cache_key in domain_results:
                return domain_results[cache_key]
            event = domain_inflight.get(cache_key)
            if event is None:
                event = threading.Event()
                domain_inflight[cache_key] = event
                owner = True
        if not owner:
            event.wait()
            with domain_cache_lock:
                if cache_key in domain_results:
                    return domain_results[cache_key]
            return process_company_details(row, session, enable_search=enable_search)
        try:
            details = process_company_details(row, session, enable_search=enable_search)
            with domain_cache_lock:
                domain_results[cache_key] = details
            return details
        finally:
            with domain_cache_lock:
                domain_inflight.pop(cache_key, None)
                event.set()

    def process(row):
        key = _company_key(row)
        name, web, country = row
        try:
            session = session_factory()
            details = process_with_domain_cache(row, session)
            status, jobs, source = details["status"], details["jobs"], details["source"]
        except Exception as exc:
            status, jobs, source = "error", [], str(exc)[:200]
            details = {"career_page_url": "", "career_page_status": "Error",
                       "career_page_discovery_method": ""}
        scraped_at = now_iso()
        out_rows = []
        for j in jobs:
            raw_description = j.get("job_description", "")
            clean_description = html_to_plain_text(raw_description)
            labeled = extract_labeled_fields(clean_description)
            experience_text = j.get("years_of_experience", "") or labeled.get("years_of_experience", "")
            derived_experience_min, derived_experience_max = experience_year_range(experience_text)
            job_source = j.get("source", "") or source
            structured_source = job_source in ("jsonld", "microdata", "hrmanager-detail")
            out_rows.append({
                "company_name": name,
                "country": country,
                "website": web,
                "career_page_url": details.get("career_page_url", ""),
                "career_page_status": details.get("career_page_status", ""),
                "career_page_discovery_method": details.get("career_page_discovery_method", ""),
                "job_title": j.get("job_title", ""),
                "job_category": j.get("job_category", ""),
                "job_location": j.get("job_location", ""),
                "posted_date": j.get("posted_date", ""),
                "application_deadline": j.get("application_deadline", ""),
                "closed_date": j.get("closed_date", ""),
                "job_status": j.get("job_status", "Active"),
                "extraction_status": "Job Detail Extracted",
                "extraction_confidence": j.get("extraction_confidence") or
                                         ("High" if structured_source else "Medium"),
                "extraction_evidence": j.get("extraction_evidence") or job_source,
                "last_checked_at": j.get("last_checked_at", ""),
                "education_stream": j.get("education_stream", ""),
                "education_type": j.get("education_type", ""),
                "education_qualification": j.get("education_qualification", ""),
                "years_of_experience_min": j.get("years_of_experience_min", "") or derived_experience_min,
                "years_of_experience_max": j.get("years_of_experience_max", "") or derived_experience_max,
                "seniority_level": j.get("seniority_level", ""),
                "employment_type": j.get("employment_type", ""),
                "skills": j.get("skills", ""),
                "description_language": detect_language(clean_description),
                "job_description": raw_description,
                "job_description_clean": clean_description,
                "job_url": j.get("job_url", ""),
                "salary_disclosed": bool(j.get("salary") or j.get("min_salary") or j.get("max_salary")),
                "salary": j.get("salary", ""),
                "min_salary": j.get("min_salary", ""),
                "max_salary": j.get("max_salary", ""),
                "currency": j.get("currency", ""),
                "source": job_source,
                "scraped_at": scraped_at,
            })
        if not out_rows:
            out_rows.append({
                "company_name": name,
                "country": country,
                "website": web,
                "career_page_url": details.get("career_page_url", ""),
                "career_page_status": details.get("career_page_status", ""),
                "career_page_discovery_method": details.get("career_page_discovery_method", ""),
                "job_status": {
                    "no_jobs": "No Jobs Found",
                    "unsupported": "Career Page Found - Extraction Unsupported",
                    "career_not_found": "Career Page Not Found",
                }.get(status, status.replace("_", " ").title()),
                "extraction_status": {
                    "no_jobs": "Explicit No-Openings Evidence",
                    "unsupported": "Extraction Unsupported",
                    "career_not_found": "Career Page Not Found",
                    "unreachable": "Website Unreachable",
                }.get(status, "Processing Error"),
                "extraction_confidence": "High" if status == "no_jobs" else "Low",
                "extraction_evidence": source,
                "source": source,
                "scraped_at": scraped_at,
            })
        writer.write_rows(out_rows)
        with counters_lock:
            counters["processed"] += 1
            counters[status] += 1
            counters["jobs"] += len(jobs)
            p = counters["processed"]
            if p % 50 == 0 or p == len(todo):
                log.info(
                    "[%d/%d] ok=%d no_jobs=%d unreach=%d err=%d jobs=%d last=%s status=%s src=%s",
                    p, len(todo), counters["ok"], counters["no_jobs"],
                    counters["unreachable"], counters["error"], counters["jobs"],
                    name, status, source,
                )
        return key

    if not todo:
        log.info("Nothing to do.")
        return counters

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process, c): c for c in todo}
        try:
            for fut in as_completed(futs):
                fut.result()
        except KeyboardInterrupt:
            log.warning("Interrupted; results so far saved. Re-run with --resume to continue.")
            ex.shutdown(wait=False, cancel_futures=True)
            raise

    log.info("Done. %s", counters)
    return counters
