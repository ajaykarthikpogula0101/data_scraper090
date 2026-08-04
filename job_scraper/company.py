import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .config import MAX_CAREER_LINKS
from .ats import (
    detect_ats_in_url,
    find_career_links,
    common_career_urls,
)
from .urlutils import ensure_https, normalize_website, hostname
from .session import ScrapeSession
from .websearch import search_company_website
from . import parsers_ats
from .parsers_generic import parse_generic

_KNOWN_GENERIC = {
    "teamtailor", "softgarden", "join", "bamboo", "icims", "taleo",
    "jobvite", "oracle", "pinpoint", "zoho", "freshteam",
    "jobadder", "bulhorn", "indeed", "adzuna",
    "avature", "talentsoft",
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
    html_l = html.lower()
    soup = BeautifulSoup(html, "html.parser")
    texts = [t for t in soup.stripped_strings][:200]
    text_blob = " ".join(texts).lower()
    blob = text_blob + " " + html_l
    for name, markers in (
        ("greenhouse", ["greenhouse", "boards.greenhouse", "grnh.se"]),
        ("lever", ["lever", "jobs.lever.co"]),
        ("smartrecruiters", ["smartrecruiters"]),
        ("workable", ["workable"]),
        ("teamtailor", ["teamtailor"]),
        ("recruitee", ["recruitee"]),
        ("breezy", ["breezy"]),
        ("jazzhr", ["applytojob", "jazzhr"]),
        ("bamboo", ["bamboohr"]),
        ("personio", ["personio"]),
        ("softgarden", ["softgarden"]),
        ("workday", ["myworkdayjobs", "workday"]),
        ("icims", ["icims"]),
        ("taleo", ["taleo", "hcm.od.taleo"]),
        ("successfactors", ["successfactors", "sap/job"]),
        ("jobvite", ["jobvite"]),
        ("join", ["join.com"]),
        ("oracle", ["oraclecloud"]),
        ("avature", ["avature"]),
        ("talentsoft", ["talentsoft", "vscdn"]),
    ):
        if any(mk in blob for mk in markers):
            return name, None
    # look at links inside the candidate page for ATS board URLs
    for a in soup.find_all("a", href=True):
        n, cap = detect_ats_in_url(a["href"])
        if n:
            return n, cap
    return None, None


def process_company(company_row, session=None, enable_search=True):
    """
    company_row: (company_name, website, country)
    Returns (status, jobs, source) where jobs is a list of dicts.
    status: "ok" | "no_jobs" | "unreachable" | "error"
    """
    name, website, country = company_row
    session = session or ScrapeSession()
    base = normalize_website(website)
    sources = []
    if not base:
        if enable_search:
            found = search_company_website(name, session, country=country)
            if not found:
                return "unreachable", [], "no_website"
            base = found[0]
            sources.append("websearch")
        else:
            return "unreachable", [], "no_website"
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
                        return "unreachable", [], ";".join(sources)
                else:
                    return "unreachable", [], ";".join(sources)
            else:
                return "unreachable", [], ""

    # 2. build candidate career urls
    candidates = []
    soup = BeautifulSoup(homepage_html or "", "html.parser")
    found_links = find_career_links(soup, homepage_url, limit=MAX_CAREER_LINKS)
    candidates.extend(found_links)
    if not found_links:
        # probe a handful of common paths
        for u, _ in common_career_urls(homepage_url)[:4]:
            candidates.append(u)

    # dedupe candidates
    seen = set()
    cands = []
    for u in candidates:
        u = u.split("#")[0].rstrip("/")
        if u and u not in seen:
            seen.add(u)
            cands.append(u)
    candidates = cands[:MAX_CAREER_LINKS]

    # 3. Try homepage as a board too
    ats, cap = _candidate_ats(session, homepage_url, homepage_html)
    if ats:
        src = ats
        sources.append(src)
        if ats in _KNOWN_GENERIC:
            jobs.extend(parse_generic(session, homepage_url))
        else:
            jobs.extend(_dispatch(session, ats, homepage_url, cap))

    # 4. process candidates
    for cand in candidates:
        if len(jobs) >= 50:
            break
        if cand in tried:
            continue
        tried.add(cand)
        ats, cap = detect_ats_in_url(cand)
        cand_html = None
        if not ats:
            cand_html = session.fetch_text(cand, timeout=25)
            if not cand_html:
                continue
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

    # 5. dedupe jobs by url/title
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
            return "no_jobs", [], ";".join(sources)
        return "ok", jobs, "jsonld-homepage"

    return "ok", jobs, ";".join(sources) or "generic"
