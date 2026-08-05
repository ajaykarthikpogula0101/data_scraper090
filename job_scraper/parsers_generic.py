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
    r"candidate\.hr-manager\.net/ApplicationInit\.aspx|/vacanc(?:y|ies)/[^/?#]+|"
    r"/open(?:-)?positions?/[^/?#]+|[?&](?:job|jobid|job_id|position|vacancy)(?:id)?=)",
    re.IGNORECASE,
)

HR_MANAGER_RE = re.compile(r"candidate\.hr-manager\.net/ApplicationInit\.aspx", re.I)
LISTING_TEXT_RE = re.compile(
    r"\b(view|see|explore|search|browse|current|open|available|all)\b.{0,30}"
    r"\b(jobs?|positions?|openings?|vacancies|opportunities)\b|"
    r"\b(jobs?|positions?|openings?|vacancies)\b.{0,20}\b(view|search|current|open|all)\b",
    re.I,
)


def _extract_listing_page_links(html, base_url, limit=8):
    soup = BeautifulSoup(html or "", "html.parser")
    links = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        text = clean_text(anchor.get_text(" ", strip=True), max_len=200)
        href = anchor.get("href", "")
        if not LISTING_TEXT_RE.search(text + " " + href):
            continue
        full = url_join(base_url, href)
        if not full or full.split("#")[0] in seen or EXCLUDE.search(full):
            continue
        seen.add(full.split("#")[0])
        links.append(full)
    return links[:limit]


def _extract_pagination_links(html, base_url, limit=5):
    soup = BeautifulSoup(html or "", "html.parser")
    links = []
    for anchor in soup.find_all("a", href=True):
        text = clean_text(anchor.get_text(" ", strip=True), max_len=80)
        rel = " ".join(anchor.get("rel") or [])
        if not ("next" in rel.lower() or re.search(
                r"^(next|next page|older|more jobs|næste|weiter|suivant|siguiente)\b", text, re.I)):
            continue
        full = url_join(base_url, anchor["href"])
        if full and full not in links:
            links.append(full)
    return links[:limit]


def _extract_listing_jobs(html, base_url):
    """Create minimal rows for explicit ATS job links on a listing page."""
    soup = BeautifulSoup(html or "", "html.parser")
    jobs = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        full = url_join(base_url, anchor["href"])
        if not full or not HR_MANAGER_RE.search(full) or full in seen:
            continue
        row = anchor.find_parent("tr")
        cells = [clean_text(cell.get_text(" ", strip=True), max_len=500)
                 for cell in row.find_all("td", recursive=False)] if row else []
        table = row.find_parent("table") if row else None
        headers = [clean_text(cell.get_text(" ", strip=True), max_len=100).lower()
                   for cell in table.find_all("th")] if table else []
        aliases = {
            "title": ("title", "job title", "position", "stilling", "titel"),
            "category": ("category", "department", "function", "kategori", "tjänst"),
            "employment_type": ("type", "employment type", "work type", "arbejdstid"),
            "location": ("location", "workplace", "arbejdssted", "sted"),
            "deadline": ("deadline", "application deadline", "closing date", "ansøgningsfrist"),
        }
        indexes = {}
        for field, names in aliases.items():
            for index, header in enumerate(headers):
                if header in names:
                    indexes[field] = index
                    break
        if cells and "title" in indexes:
            value = lambda field: (cells[indexes[field]]
                                   if field in indexes and indexes[field] < len(cells) else "")
            title = value("title")
            category = value("category")
            employment_type = value("employment_type")
            location = value("location")
            deadline = value("deadline")
            mapping_confidence = "High"
        elif len(cells) >= 5:
            title, category, employment_type, location, deadline = cells[:5]
            mapping_confidence = "Low"
        else:
            title = clean_text(anchor.get_text(" ", strip=True), max_len=500)
            title = re.sub(r"^Ansøgningsfrist:\s*\d{1,2}\.\s*[A-Za-zæøåÆØÅ]+\.\s*\d{4}\s*", "", title,
                           flags=re.I)
            category = employment_type = location = deadline = ""
            mapping_confidence = "Low"
        if not title:
            continue
        job = empty_job()
        job["job_title"] = title
        job["job_category"] = "" if category in ("-", "–") else category
        job["job_location"] = location
        job["employment_type"] = employment_type
        job["application_deadline"] = "" if "invalid" in deadline.lower() else parse_date(deadline)
        job["job_url"] = full
        job["job_status"] = "Active"
        job["source"] = "hrmanager-listing"
        job["extraction_confidence"] = mapping_confidence
        job["extraction_evidence"] = "header-mapped listing" if mapping_confidence == "High" else "positional listing"
        jobs.append(job)
        seen.add(full)
    return jobs


def _meta_content(soup, key):
    tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
    return clean_text(tag.get("content"), max_len=1000) if tag else ""


def _parse_hrmanager_date(text):
    match = re.search(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b", text or "")
    if match:
        return "%s-%02d-%02d" % (match.group(3), int(match.group(2)), int(match.group(1)))
    return parse_date(text)


def _hrmanager_labeled_value(container, labels):
    if not container:
        return ""
    text = clean_text(container.get_text(" ", strip=True), max_len=1000)
    for label in labels:
        text = re.sub(r"^%s\s*" % re.escape(label), "", text, flags=re.I)
    return text.strip()


def _parse_hrmanager_detail(html, url, seed=None):
    """Extract explicit HR-Manager/Talentech detail fields without inference."""
    soup = BeautifulSoup(html or "", "html.parser")
    job = empty_job()
    if seed:
        job.update(seed)
    title = _meta_content(soup, "og:title") or _meta_content(soup, "twitter:title")
    if title:
        job["job_title"] = title
    description = soup.find(id="AdvertisementInnerContent")
    if description:
        job["job_description"] = str(description)
    posted = _meta_content(soup, "article:published_time")
    if posted:
        job["posted_date"] = parse_date(posted)
    deadline_box = soup.select_one(".frist")
    if deadline_box:
        deadline = _parse_hrmanager_date(deadline_box.get_text(" ", strip=True))
        if deadline:
            job["application_deadline"] = deadline
    category_box = soup.select_one(".jobtype")
    if category_box and not job.get("job_category"):
        job["job_category"] = _hrmanager_labeled_value(
            category_box, ("Tjänst", "Stilling", "Jobtype", "Category"))
    keywords = _meta_content(soup, "keywords")
    parts = [part.strip() for part in keywords.split(",") if part.strip()]
    if len(parts) >= 3:
        if not job.get("job_location"):
            job["job_location"] = ", ".join(parts[1:-1])
    job["job_url"] = url
    job["job_status"] = "Active"
    job["source"] = "hrmanager-detail"
    return job


def _enrich_hrmanager_jobs(session, jobs):
    enriched = []
    for job in jobs:
        if not HR_MANAGER_RE.search(job.get("job_url") or ""):
            enriched.append(job)
            continue
        detail = session.fetch_text(job["job_url"], timeout=30)
        enriched.append(_parse_hrmanager_detail(detail, job["job_url"], seed=job) if detail else job)
    return enriched


def _labeled_page_value(soup, labels):
    wanted = {label.lower() for label in labels}
    for node in soup.find_all(["dt", "th", "strong", "b", "span", "div"]):
        label = clean_text(node.get_text(" ", strip=True), max_len=100).rstrip(":").lower()
        if label not in wanted:
            continue
        if node.name == "dt":
            value_node = node.find_next_sibling("dd")
        elif node.name == "th":
            value_node = node.find_next_sibling("td")
        else:
            value_node = node.find_next_sibling()
        if value_node:
            value = clean_text(value_node.get_text(" ", strip=True), max_len=500)
            if value:
                return value
    return ""


def _parse_generic_detail(html, url):
    """Evidence-based fallback for unfamiliar but conventional job pages."""
    soup = BeautifulSoup(html or "", "html.parser")
    title = ""
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title:
        title = clean_text(og_title.get("content"), max_len=500)
    if not title:
        heading = soup.find("h1")
        title = clean_text(heading.get_text(" ", strip=True), max_len=500) if heading else ""
    selectors = ["[class*='job-description']", "[id*='job-description']",
                 "[class*='jobDescription']", "article", "main"]
    description = next((soup.select_one(selector) for selector in selectors if soup.select_one(selector)), None)
    if not title or not description:
        return None
    description_text = clean_text(description.get_text(" ", strip=True), max_len=30000)
    page_text = clean_text(soup.get_text(" ", strip=True), max_len=50000)
    application_signal = re.search(r"\b(apply|application|submit your cv|send your cv|ansøg|bewerben)\b", page_text, re.I)
    if len(description_text) < 100 or not application_signal:
        return None
    job = empty_job()
    job["job_title"] = title
    job["job_description"] = str(description)
    job["job_url"] = url
    job["job_status"] = "Active"
    job["job_location"] = _labeled_page_value(soup, ("location", "work location", "workplace", "arbejdssted"))
    job["job_category"] = _labeled_page_value(soup, ("category", "department", "team", "function"))
    job["employment_type"] = _labeled_page_value(
        soup, ("employment type", "job type", "work type", "schedule", "arbejdstid"))
    job["application_deadline"] = parse_date(_labeled_page_value(
        soup, ("application deadline", "closing date", "deadline", "ansøgningsfrist")))
    labeled = extract_labeled_fields(description_text)
    for field in ("employment_type", "education_qualification", "seniority_level"):
        if not job.get(field) and labeled.get(field):
            job[field] = labeled[field]
    if labeled.get("salary") and re.search(r"\d", labeled["salary"]):
        from .fields import salary_breakdown, currency_from_salary
        display, minimum, maximum = salary_breakdown(labeled["salary"])
        job.update({"salary": display, "min_salary": minimum, "max_salary": maximum,
                    "currency": currency_from_salary(labeled["salary"])})
    job["source"] = "generic-detail"
    return job

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
    listing_jobs = []
    listing_queue = _extract_listing_page_links(html, url)
    listing_seen = set()
    while listing_queue and len(listing_seen) < 10:
        listing_url = listing_queue.pop(0)
        if listing_url.rstrip("/") == url.rstrip("/"):
            continue
        if listing_url in listing_seen:
            continue
        listing_seen.add(listing_url)
        listing_html = session.fetch_text(listing_url, timeout=35)
        if not listing_html:
            continue
        parsed_listing = parse_jsonld_jobs(listing_html, listing_url)
        if not parsed_listing:
            parsed_listing = parse_microdata_jobs(listing_html, listing_url)
        if not parsed_listing:
            parsed_listing = _extract_listing_jobs(listing_html, listing_url)
        listing_jobs.extend(parsed_listing)
        job_links.extend(_extract_job_links(listing_html, listing_url))
        listing_queue.extend(_extract_pagination_links(listing_html, listing_url))
    job_links = list(dict.fromkeys(job_links))[:MAX_JOB_DETAIL_PAGES]
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
        if not parsed:
            generic_job = _parse_generic_detail(detail, link)
            parsed = [generic_job] if generic_job else []
        detail_jobs.extend(parsed)
    combined = _merge_jobs([jobs, listing_jobs, detail_jobs])
    combined = _enrich_hrmanager_jobs(session, combined)
    for j in combined:
        if not j["source"]:
            j["source"] = "generic"
        labeled = extract_labeled_fields(j.get("job_description") or "")
        for key in ("employment_type", "seniority_level", "education_qualification",
                    "years_of_experience", "salary"):
            if not j.get(key) and labeled.get(key):
                j[key] = labeled[key]
    return combined
