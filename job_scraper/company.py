import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .config import MAX_CAREER_LINKS
from .ats import (
    detect_ats_in_url,
    find_career_links,
    common_career_urls,
    validate_career_page,
)
from .urlutils import ensure_https, normalize_website, hostname
from .session import ScrapeSession
from .websearch import search_company_website, search_company_career_pages
from . import parsers_ats
from .parsers_generic import parse_generic

_KNOWN_GENERIC = {
    "teamtailor", "softgarden", "join", "bamboo", "icims", "taleo",
    "jobvite", "oracle", "pinpoint", "zoho", "freshteam",
    "jobadder", "bulhorn", "indeed", "adzuna",
    "avature", "talentsoft", "hrmanager",
}

_API_PARSERS = {
    "greenhouse": parsers_ats.parse_greenhouse,
    "lever": parsers_ats.parse_lever,
    "smartrecruiters": parsers_ats.parse_smartrecruiters,
    "workable": parsers_ats.parse_workable,
    "recruitee": parsers_ats.parse_recruitee,
    "breezy": parsers_ats.parse_breezy,
    "jazzhr": parsers_ats.parse_jazzhr,
    "personio": parsers_ats.parse_personio,
    "workday": parsers_ats.parse_workday,
    "successfactors": parsers_ats.parse_successfactors,
}


def _ats_info(ats, url, captured):
    info = {"board": captured or "", "base_url": url}
    if ats == "personio":
        m = re.search(r"jobs\.personio\.([a-z]{2}(?:\.[a-z]{2})?)", url)
        info["tld"] = m.group(1) if m else "de"
    elif ats == "workday":
        info = parsers_ats.workday_info_from_url(url)
    elif ats == "smartrecruiters":
        if not captured:
            p = urlparse(url)
            segs = [s for s in p.path.split("/") if s]
            info["board"] = segs[0] if segs else ""
    return info


def _dispatch(session, ats, url, captured):
    info = _ats_info(ats, url, captured)
    parser = _API_PARSERS.get(ats)
    if parser:
        try:
            return parser(session, info)
        except Exception:
            return []
    return []


def _candidate_ats(session, candidate_url, html):
    """Detect ATS from URL, then from page content/links."""
    ats, captured = detect_ats_in_url(candidate_url)
    if ats:
        return ats, captured
    if not html:
        return None, None
    soup = BeautifulSoup(html, "html.parser")
    # look at links inside the candidate page for ATS board URLs
    for a in soup.find_all("a", href=True):
        from .urlutils import url_join
        n, cap = detect_ats_in_url(url_join(candidate_url, a["href"]))
        if n:
            return n, cap
    # Embedded ATS URLs in scripts are useful; bare vendor words are not.
    embedded = re.search(
        r"https?://[^\"'\s<>]+(?:greenhouse\.io|lever\.co|smartrecruiters\.com|"
        r"workable\.com|teamtailor\.com|recruitee\.com|breezy\.hr|applytojob\.com|"
        r"bamboohr\.com|personio\.[a-z.]+|myworkdayjobs\.com|taleo\.net|jobvite\.com)[^\"'\s<>]*",
        html, re.I)
    if embedded:
        return detect_ats_in_url(embedded.group(0))
    return None, None


def process_company_details(company_row, session=None, enable_search=True):
    """
    company_row: (company_name, website, country)
    Return status/jobs plus auditable career-page discovery metadata.
    """
    name, website, country = company_row
    session = session or ScrapeSession()
    base = normalize_website(website)
    sources = []
    discovery = {
        "career_page_url": "",
        "career_page_status": "Not Found",
        "career_page_discovery_method": "",
    }

    def result(status, jobs, source):
        return {"status": status, "jobs": jobs, "source": source, **discovery}

    if not base:
        if enable_search:
            found = search_company_website(name, session, country=country)
            if not found:
                return result("unreachable", [], "no_website")
            base = found[0]
            sources.append("websearch")
        else:
            return result("unreachable", [], "no_website")
    jobs = []
    tried = set()
    homepage_html = None

    # 1. fetch homepage
    resp = session.fetch(base, timeout=20)
    homepage_url = base
    if resp is not None and resp.status_code < 400:
        homepage_url = resp.url or base
        ctype = resp.headers.get("Content-Type", "")
        if "json" not in ctype.lower():
            homepage_html = resp.text
    elif resp is None:
        # try http:// variant
        http_url = "http://" + urlparse(base).netloc
        resp2 = session.fetch(http_url, timeout=15)
        if resp2 is not None and resp2.status_code < 400:
            homepage_url = resp2.url or http_url
            homepage_html = resp2.text
        else:
            if enable_search:
                found = search_company_website(name, session, country=country)
                if found and hostname(found[0]) != hostname(base):
                    sources.append("websearch")
                    resp3 = session.fetch(found[0], timeout=20)
                    if resp3 is not None and resp3.status_code < 400:
                        base = found[0]
                        homepage_url = resp3.url or base
                        ctype = resp3.headers.get("Content-Type", "")
                        if "json" not in ctype.lower():
                            homepage_html = resp3.text
                    else:
                        return result("unreachable", [], ";".join(sources))
                else:
                    return result("unreachable", [], ";".join(sources))
            else:
                return result("unreachable", [], "")

    # 2. build candidate career urls
    candidates = []
    soup = BeautifulSoup(homepage_html or "", "html.parser")
    found_links = find_career_links(soup, homepage_url, limit=MAX_CAREER_LINKS)
    candidates.extend(found_links)
    candidate_methods = {url: "homepage_link" for url in found_links}
    if not found_links:
        # probe a handful of common paths
        for u, _ in common_career_urls(homepage_url)[:4]:
            candidates.append(u)
            candidate_methods[u] = "common_path_probe"
        if enable_search:
            for u in search_company_career_pages(name, homepage_url, session, country=country):
                candidates.append(u)
                candidate_methods[u] = "web_search"

    # dedupe candidates
    seen = set()
    cands = []
    for u in candidates:
        method = candidate_methods.get(u, "homepage_link")
        normalized = u.split("#")[0].rstrip("/")
        if normalized and normalized not in seen:
            seen.add(normalized)
            cands.append(normalized)
            candidate_methods[normalized] = method
    candidates = cands[:MAX_CAREER_LINKS]

    # 3. Process explicit career candidates. A vendor marker on a homepage is
    # not enough to call the homepage a career page.
    for cand in candidates:
        if len(jobs) >= 50:
            break
        if cand in tried:
            continue
        tried.add(cand)
        ats, cap = detect_ats_in_url(cand)
        cand_html = session.fetch_text(cand, timeout=25)
        if not cand_html and not ats:
            continue
        if not discovery["career_page_url"] and (
                (cand_html and validate_career_page(cand, cand_html, homepage_url)) or ats):
            discovery.update({
                "career_page_url": cand,
                "career_page_status": "Validated",
                "career_page_discovery_method": candidate_methods.get(cand, "homepage_link"),
            })
        if not ats:
            ats, cap = _candidate_ats(session, cand, cand_html)
        if ats:
            src = ats
            if src not in sources:
                sources.append(src)
            if ats in _KNOWN_GENERIC:
                jobs.extend(parse_generic(session, cand))
            else:
                jobs.extend(_dispatch(session, ats, cand, cap))
        elif cand_html:
            # generic: parse the candidate page directly
            jobs.extend(parse_generic(session, cand))

    # 4. dedupe jobs by url/title
    uniq = {}
    for j in jobs:
        key = (j.get("job_url") or "").strip() or (j.get("job_title") or "").strip()
        if not key:
            continue
        if key in uniq:
            continue
        uniq[key] = j
    jobs = list(uniq.values())[:200]

    if not jobs:
        # final fallback: JSON-LD on homepage
        if homepage_html:
            from .parsers_jsonld import parse_jsonld_jobs
            jobs = parse_jsonld_jobs(homepage_html, homepage_url)
        if not jobs:
            return result("no_jobs", [], ";".join(sources))
        if not discovery["career_page_url"]:
            discovery.update({
                "career_page_url": homepage_url,
                "career_page_status": "Validated",
                "career_page_discovery_method": "homepage_jobposting",
            })
        return result("ok", jobs, "jsonld-homepage")

    return result("ok", jobs, ";".join(sources) or "generic")


def process_company(company_row, session=None, enable_search=True):
    """Backward-compatible three-value company processing API."""
    details = process_company_details(company_row, session=session, enable_search=enable_search)
    return details["status"], details["jobs"], details["source"]
