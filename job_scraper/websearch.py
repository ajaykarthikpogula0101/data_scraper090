import logging
import re
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from requests import Session as _ReqSession

from .ats import registrable_domain, detect_ats_in_url, is_career_link
from .config import SEARCH_TIMEOUT
from .urlutils import ensure_https, hostname

log = logging.getLogger("job_scraper")

_SOCIAL_HOSTS = re.compile(
    r"(linkedin\.com|facebook\.com|twitter\.com|instagram\.com|youtube\.com|"
    r"wikipedia\.org|glassdoor\.com|indeed\.com|pinterest\.com|xing\.com|"
    r"crunchbase\.com|zoominfo\.com|trustpilot\.com|yelp\.com|linkedin\.co|"
    r"yellowpages\.com|find-and-update\.company|dun\.com|glassdoor\.co|"
    r"companyhouse|facebook\.co|reuters\.com|britannica\.com)", re.IGNORECASE,
)

_SKIP_SUBDOMAIN = (
    "blog", "news", "m", "shop", "store", "forum", "mail", "wiki",
    "support", "help", "docs", "login", "app", "secure", "status",
)

_STOPWORDS = {
    "the", "and", "ltd", "llc", "gmbh", "inc", "corp", "co", "sa", "oy",
    "ab", "bv", "nv", "plc", "limited", "company", "group", "holding",
    "srl", "ag", "kg", "sas", "spa", "pty", "pvt", "private", "sdn", "bhd",
    "ooo", "zao", "doo", "sro", "kft", "sa", "corp", "corporation", "gmbh",
}

_MIN_SCORE = 20


def _tokens(name):
    name = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    return [t for t in name.split() if t and t not in _STOPWORDS and len(t) > 1]


def _root_url(dom):
    return "https://www." + dom


def _score(dom, tokens):
    dom = (dom or "").lower()
    if not dom or dom.count(".") < 1:
        return -1000
    if _SOCIAL_HOSTS.search(dom):
        return -1000
    s = 0
    if not tokens:
        return 0
    for t in tokens:
        if t in dom:
            s += 20
        if len(t) > 3 and (dom.startswith(t) or dom.endswith(t)):
            s += 10
    return s


def _search_bing_rss(session, query):
    url = "https://www.bing.com/search?q=" + quote_plus(query) + "&format=rss"
    r = session.fetch(url, timeout=SEARCH_TIMEOUT)
    if r is None or r.status_code >= 400:
        return []
    txt = r.text
    if "<item>" not in txt:
        return []
    out = []
    for m in re.finditer(r"<item>.*?<link>(.*?)</link>.*?</item>", txt, re.S | re.I):
        link = m.group(1).strip()
        if link.startswith("http"):
            out.append(link)
    return out


def _search_duckduckgo(session, query):
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    html = session.fetch_text(url, timeout=SEARCH_TIMEOUT)
    if not html or "anomaly" in html.lower():
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        if "uddg=" in href:
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(href).query)
            href = q.get("uddg", [""])[0]
        if href.startswith("http"):
            out.append(href)
    return out


def search_company_website(name, session, country=None, limit=5):
    """Search for the official website of a company by name.

    Returns a list of candidate website roots (best first), or [].
    """
    tokens = _tokens(name)
    queries = ['"%s" official website' % name]
    if country:
        queries.append('"%s" official website %s' % (name, country))
    if not tokens:
        return []

    results = []
    for q in queries:
        results.extend(_search_bing_rss(session, q))
    if not results:
        for q in queries:
            results.extend(_search_duckduckgo(session, q))
    if not results:
        return []

    scored = {}
    for r in results:
        try:
            dom = registrable_domain(hostname(ensure_https(r)))
        except Exception:
            continue
        if not dom:
            continue
        score = _score(dom, tokens)
        if score >= _MIN_SCORE and dom not in scored:
            scored[dom] = score

    ranked = sorted(scored.items(), key=lambda x: -x[1])
    return [_root_url(dom) for dom, _ in ranked[:limit]]


def search_company_career_pages(name, official_url, session, country=None, limit=3):
    """Find likely official career pages while rejecting unrelated job sites."""
    official_domain = registrable_domain(hostname(ensure_https(official_url)))
    if not official_domain:
        return []
    query_name = '"%s" careers jobs' % name
    queries = ["site:%s careers jobs" % official_domain, query_name]
    if country:
        queries.append(query_name + " " + country)
    results = []
    for query in queries:
        results.extend(_search_bing_rss(session, query))
        # Irrelevant Bing results used to suppress this second engine entirely.
        # Merge both channels and filter afterward instead.
        results.extend(_search_duckduckgo(session, query))
    accepted = []
    seen = set()
    company_tokens = _tokens(name)
    for url in results:
        url = ensure_https(url).split("#")[0]
        if not url or url in seen:
            continue
        result_domain = registrable_domain(hostname(url))
        ats, captured = detect_ats_in_url(url)
        same_domain = result_domain == official_domain
        ats_matches_company = bool(ats and captured and any(token in captured.lower() for token in company_tokens))
        if not ((same_domain and is_career_link("", url)) or ats_matches_company):
            continue
        seen.add(url)
        accepted.append(url)
        if len(accepted) >= limit:
            break
    return accepted


def search_company_job_pages(name, official_url, session, country=None, limit=8):
    """Search by company name for likely career boards or individual jobs.

    Results are candidates only. The caller must fetch the page and verify the
    company identity before exporting a job.
    """
    official_domain = registrable_domain(hostname(ensure_https(official_url)))
    queries = ['"%s" jobs careers' % name, '%s jobs careers' % name]
    if country:
        queries[0] += " " + country
    if official_domain:
        queries.insert(0, "site:%s (jobs OR careers OR vacancies OR stellenangebote)" % official_domain)
    results = []
    for query in queries[:3]:
        results.extend(_search_bing_rss(session, query))
        results.extend(_search_duckduckgo(session, query))
    tokens = _tokens(name)
    accepted = []
    seen = set()
    for raw_url in results:
        url = ensure_https(raw_url).split("#")[0]
        if not url or url in seen:
            continue
        result_domain = registrable_domain(hostname(url))
        ats, captured = detect_ats_in_url(url)
        same_domain_job = bool(result_domain == official_domain and is_career_link("", url))
        ats_company_match = bool(
            ats and captured and any(len(token) >= 3 and token in captured.lower() for token in tokens)
        )
        job_board_detail = bool(
            re.search(r"(?:linkedin|indeed|glassdoor|stepstone|monster|ziprecruiter|xing)\.",
                      hostname(url), re.I)
            and re.search(r"/(?:jobs?|viewjob|job-listing|stellenangebote?)/|[?&](?:jk|jobId)=", url, re.I)
        )
        if not (same_domain_job or ats_company_match or job_board_detail):
            continue
        seen.add(url)
        accepted.append(url)
        if len(accepted) >= limit:
            break
    return accepted
