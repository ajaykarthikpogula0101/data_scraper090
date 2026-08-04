import re
import json
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .fields import clean_text, join_list, parse_date, to_list, empty_job, extract_labeled_fields
from .parsers_jsonld import parse_jsonld_jobs, parse_microdata_jobs
from .urlutils import url_join
from .config import MAX_JOB_DETAIL_PAGES, MAX_JOBS_PER_COMPANY

JOB_LINK_RE = re.compile(
    r"(/job[s]?/[a-zA-Z0-9_\-]+|/jobdetail\b|jobdetail\.ftl|jobid\s*[=:]|"
    r"job-req\b|/position[s]?/[a-zA-Z0-9_\-]+|/officerole|careers/\d+|"
    r"/job[s]?\.php|/viewjob\b|/careers/[a-z0-9\-]+/job|"
    r"candidate\.hr-manager\.net/ApplicationInit\.aspx)",
    re.IGNORECASE,
)

HR_MANAGER_RE = re.compile(r"candidate\.hr-manager\.net/ApplicationInit\.aspx", re.I)


def _extract_listing_jobs(html, base_url):
    """Create minimal rows for explicit ATS job links on a listing page."""
    soup = BeautifulSoup(html or "", "html.parser")
    jobs = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        full = url_join(base_url, anchor["href"])
        if not full or not HR_MANAGER_RE.search(full) or full in seen:
            continue
        title = clean_text(anchor.get_text(" ", strip=True), max_len=500)
        title = re.sub(r"^Ansøgningsfrist:\s*\d{1,2}\.\s*[A-Za-zæøåÆØÅ]+\.\s*\d{4}\s*", "", title,
                       flags=re.I)
        if not title:
            continue
        job = empty_job()
        job["job_title"] = title
        job["job_url"] = full
        job["job_status"] = "Active"
        job["source"] = "hrmanager-listing"
        jobs.append(job)
        seen.add(full)
    return jobs

EXCLUDE = re.compile(
    r"(javascript:|mailto:|tel:|#|\.(jpg|jpeg|png|gif|svg|css|js|pdf|zip|mp4)"
    r"|/wp-content/|/assets/|/static/|/img/|/images/|facebook|linkedin|twitter|"
    r"instagram|youtube|\.xml$|/feed|/api/|/login|/signup|/admin|/news|/blog|"
    r"/about|/contact|/team|/events|/press|/faq)",
    re.IGNORECASE,
)


def _extract_job_links(html, base_url, limit=MAX_JOB_DETAIL_PAGES):
    soup = BeautifulSoup(html or "", "html.parser")
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = " ".join(a.get_text(" ", strip=True).split()).lower()
        if not JOB_LINK_RE.search(href):
            continue
        if EXCLUDE.search(href):
            continue
        full = url_join(base_url, href)
        if not full:
            continue
        if full.split("#")[0] in seen:
            continue
        seen.add(full.split("#")[0])
        score = 0
        if text and ("apply" in text or "view" in text or "more" in text):
            score += 1
        links.append((score, full))
    links.sort(reverse=True)
    return [u for _, u in links[:limit]]


def _merge_jobs(list_of_lists):
    seen = {}
    for lst in list_of_lists:
        for j in lst:
            key = j.get("job_url") or j.get("job_title")
            if key in seen:
                continue
            seen[key] = j
    return list(seen.values())[:MAX_JOBS_PER_COMPANY]


def parse_generic(session, url):
    """Best-effort parser: JSON-LD on the page + crawl job detail links."""
    html = session.fetch_text(url, timeout=35)
    if not html:
        return []
    jobs = parse_jsonld_jobs(html, url)
    if not jobs:
        jobs = parse_microdata_jobs(html, url)
    if not jobs:
        jobs = _extract_listing_jobs(html, url)
    job_links = _extract_job_links(html, url)
    detail_jobs = []
    for link in job_links[:MAX_JOB_DETAIL_PAGES]:
        detail = session.fetch_text(link, timeout=25)
        if not detail:
            continue
        parsed = parse_jsonld_jobs(detail, link)
        if not parsed:
            parsed = parse_microdata_jobs(detail, link)
        if not parsed:
            parsed = _extract_listing_jobs(detail, link)
        detail_jobs.extend(parsed)
    combined = _merge_jobs([jobs, detail_jobs])
    for j in combined:
        if not j["source"]:
            j["source"] = "generic"
        labeled = extract_labeled_fields(j.get("job_description") or "")
        for key in ("employment_type", "seniority_level", "education_qualification",
                    "years_of_experience", "salary"):
            if not j.get(key) and labeled.get(key):
                j[key] = labeled[key]
    return combined
