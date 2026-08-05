"""Re-check active job URLs and mark postings that are no longer available."""

import argparse
import csv
import os
import re
import tempfile
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

from .session import ScrapeSession


CLOSED_RE = re.compile(
    r"\b(position|job|role|vacancy|posting)\s+(?:is\s+)?(?:no longer available|closed|filled|expired)\b"
    r"|\bapplications? (?:are )?closed\b|\bthis job has expired\b",
    re.IGNORECASE,
)


def _has_jobposting_jsonld(html_text):
    soup = BeautifulSoup(html_text or "", "html.parser")
    for script in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        if re.search(r'"?@type"?\s*:\s*"JobPosting"', script.get_text() or "", re.I):
            return True
    return False


def classify_response(response, original_url):
    """Return (is_closed, reason) using only observable page evidence."""
    if response is None:
        return False, "request_failed"
    if response.status_code in (404, 410):
        return True, "http_%s" % response.status_code
    if response.status_code >= 400:
        return False, "http_%s" % response.status_code
    text = response.text or ""
    final_url = getattr(response, "url", original_url) or original_url
    redirected = final_url.rstrip("/") != (original_url or "").rstrip("/")
    closed_copy = bool(CLOSED_RE.search(BeautifulSoup(text, "html.parser").get_text(" ", strip=True)))
    if closed_copy:
        return True, "closed_message"
    if redirected and _looks_like_generic_landing(final_url, original_url):
        return True, "redirected_to_listing_root"
    # Missing JSON-LD is inconclusive: many active ATS detail pages never
    # publish JobPosting markup.
    return (False, "active") if _has_jobposting_jsonld(text) else (False, "active_or_inconclusive")


def _looks_like_generic_landing(final_url, original_url):
    final = urlparse(final_url)
    original = urlparse(original_url)
    final_path = final.path.lower().rstrip("/")
    original_path = original.path.lower().rstrip("/")
    roots = {"", "/career", "/careers", "/jobs", "/vacancies", "/open-positions",
             "/opportunities", "/join-us", "/work-with-us"}
    original_specific = (original_path not in roots and
                         (len([part for part in original_path.split("/") if part]) >= 2 or
                          bool(original.query)))
    return original_specific and final_path in roots


def _due(last_checked, interval_days, now):
    if not last_checked:
        return True
    try:
        checked = datetime.fromisoformat(last_checked.replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        return now - checked >= timedelta(days=interval_days)
    except ValueError:
        return True


def _atomic_write(path, rows, fieldnames):
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=".recrawl_", suffix=".csv", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def recrawl_csv(path, interval_days=7, session=None, now=None):
    current = now or datetime.now(timezone.utc)
    client = session or ScrapeSession()
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "last_checked_at" not in fieldnames:
        fieldnames.append("last_checked_at")
    stats = {"checked": 0, "closed": 0, "skipped": 0, "failed": 0}
    for row in rows:
        if (row.get("job_status") or "").strip().lower() != "active" or not row.get("job_url"):
            stats["skipped"] += 1
            continue
        if not _due(row.get("last_checked_at", ""), interval_days, current):
            stats["skipped"] += 1
            continue
        response = client.fetch(row["job_url"], timeout=20, allow_redirects=True)
        is_closed, reason = classify_response(response, row["job_url"])
        if reason == "request_failed":
            stats["failed"] += 1
            continue
        row["last_checked_at"] = current.strftime("%Y-%m-%dT%H:%M:%SZ")
        stats["checked"] += 1
        if is_closed:
            row["job_status"] = "Closed"
            row["closed_date"] = current.strftime("%Y-%m-%d")
            stats["closed"] += 1
    _atomic_write(path, rows, fieldnames)
    return stats


def main():
    parser = argparse.ArgumentParser(description="Re-check active job posting URLs")
    parser.add_argument("csv_file")
    parser.add_argument("--interval-days", type=int, default=7)
    args = parser.parse_args()
    print(recrawl_csv(args.csv_file, interval_days=args.interval_days))


if __name__ == "__main__":
    main()
