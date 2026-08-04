import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .urlutils import ensure_https, url_join

# ---------------------------------------------------------------------------
# ATS fingerprints found inside URLs.
# ---------------------------------------------------------------------------
ATS_URL_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|careers)\.greenhouse\.io/([a-zA-Z0-9\-_.]+)")),
    ("lever", re.compile(r"(?:jobs|careers)\.lever\.co/([a-zA-Z0-9\-_.]+)")),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([a-zA-Z0-9\-_]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([a-zA-Z0-9\-_]+)")),
    ("teamtailor", re.compile(r"([a-zA-Z0-9\-_]+)\.teamtailor\.com")),
    ("recruitee", re.compile(r"([a-zA-Z0-9\-_]+)\.recruitee\.com")),
    ("breezy", re.compile(r"([a-zA-Z0-9\-_]+)\.breezy\.hr")),
    ("jazzhr", re.compile(r"([a-zA-Z0-9\-_]+)\.(?:applytojob\.com|jazzhr\.com|jazz\.co)")),
    ("bamboo", re.compile(r"([a-zA-Z0-9\-_]+)\.bamboohr\.com")),
    ("personio", re.compile(r"([a-zA-Z0-9\-_]+)\.jobs\.personio\.(?:de|com|eu|at|es|fr|it|nl|uk|co\.uk|be|ch|dk|se|no|pl|cz)")),
    ("softgarden", re.compile(r"([a-zA-Z0-9\-_]+)\.softgarden\.io")),
    ("workday", re.compile(r"([a-zA-Z0-9\-_]+)\.wd\d+\.myworkdayjobs\.com")),
    ("icims", re.compile(r"(?:jobs\.icims\.com|(?:[a-zA-Z0-9\-_]+)\.icims\.com)")),
    ("taleo", re.compile(r"([a-zA-Z0-9\-_]+)\.taleo\.net")),
    ("successfactors", re.compile(r"(?:(?:[a-zA-Z0-9\-_]+)\.)?(?:jobs\.sap\.com|successfactors\.(?:eu|com|net)|sapsf\.eu|sap\.jobs)")),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/([a-zA-Z0-9\-_]+)")),
    ("join", re.compile(r"join\.com/(?:companies/)?([a-zA-Z0-9\-_]+)")),
    ("oracle", re.compile(r"([a-zA-Z0-9\-_]+)\.(?:oraclecloud\.com|ce\.hcm\.od\.taleo\.net)")),
    ("pinpoint", re.compile(r"([a-zA-Z0-9\-_]+)\.pinpointhq\.com")),
    ("zoho", re.compile(r"([a-zA-Z0-9\-_]+)\.zohorecruit\.(?:com|eu|in|jp|com\.au|com\.mx)")),
    ("freshteam", re.compile(r"([a-zA-Z0-9\-_]+)\.freshteam\.com")),
    ("jobadder", re.compile(r"([a-zA-Z0-9\-_]+)\.jobadder\.com")),
    ("bulhorn", re.compile(r"(?:bulhorn|vault)\.com")),
    ("workable", re.compile(r"([a-zA-Z0-9\-_]+)\.workable\.com")),
]

# ATS fingerprints found in HTML page content.
ATS_HTML_MARKERS = [
    ("greenhouse", ["boards.greenhouse.io", "grnh.se", "greenhouse.io", "GHJob"]),
    ("lever", ["jobs.lever.co", "lever.co", "LeverJob", "lever-post"]),
    ("smartrecruiters", ["smartrecruiters.com", "SmartRecruiters"]),
    ("workable", ["apply.workable.com", "Workable"]),
    ("teamtailor", ["teamtailor.com", "Teamtailor", "teamtailor"]),
    ("recruitee", ["recruitee.com", "Recruitee"]),
    ("breezy", ["breezy.hr", "BreezyHR"]),
    ("jazzhr", ["applytojob.com", "jazzhr.com", "jazz.co", "JazzHR"]),
    ("bamboo", ["bamboohr.com", "BambooHR", "bamboohr"]),
    ("personio", ["jobs.personio", "Personio"]),
    ("softgarden", ["softgarden.io", "Softgarden", "softgarden"]),
    ("workday", ["myworkdayjobs.com", "Workday", "wd3.myworkdayjobs", "workday"]),
    ("icims", ["icims.com", "iCIMS", "icims"]),
    ("taleo", ["taleo.net", "Taleo", "taleo"]),
    ("successfactors", ["successfactors", "jobs.sap.com", "SuccessFactors"]),
    ("jobvite", ["jobvite.com", "Jobvite", "jvite"]),
    ("join", ["join.com", "JOIN.com"]),
    ("oracle", ["oraclecloud.com", "ce.hcm.od.taleo.net"]),
    ("pinpoint", ["pinpointhq.com", "Pinpoint"]),
    ("zoho", ["zohorecruit", "Zoho Recruit"]),
    ("freshteam", ["freshteam.com", "Freshteam"]),
    ("jobadder", ["jobadder.com", "JobAdder"]),
    ("bulhorn", ["bulhorn.com"]),
    ("indeed", ["indeed.com"]),
    ("adzuna", ["adzuna"]),
]

CAREER_KEYWORDS = [
    r"career",
    r"careers",
    r"jobs?",
    r"join[\s_-]*us",
    r"joinus",
    r"vacancies?",
    r"job-openings",
    r"open-positions",
    r"employment",
    r"working[-\s_]*(with|for|at)",
    r"work[-\s_]*with[-\s_]*us",
    r"we[-\s_]*are[-\s_]*hiring",
    r"hiring",
    r"job[s]?\b",
    r"opportunit",
    r"stellenangebot",
    r"karriere",
    r"trabaj[aá]",
    r"empleo",
    r"offres[-\s]*d[’']emploi",
    r"emploi",
    r"recruit",
    r"recrut",
    r"praca",
    r"kariera",
    r"empleo",
    r"vagas",
    r"carreira",
    r"應聘|招聘",
    r"jobs\.?page",
]

EXCLUDE_PATH_RE = re.compile(
    r"(javascript:|mailto:|tel:|#|\.(jpg|jpeg|png|gif|svg|css|js|pdf|zip|mp4|webp)"
    r"|/wp-content/|/assets/|/static/|/img/|/images/|facebook|linkedin|twitter|instagram"
    r"|youtube|\.xml$|\.json$|/feed|/api/|/login|/signup|/admin)",
    re.IGNORECASE,
)


def detect_ats_in_url(url):
    url = ensure_https(url or "")
    for ats, pat in ATS_URL_PATTERNS:
        m = pat.search(url)
        if m:
            cap = m.group(1) if m.groups() else ""
            return ats, cap
    return None, None


def _detect_ats_in_text(text):
    if not text:
        return None
    low = text
    for ats, markers in ATS_HTML_MARKERS:
        for mk in markers:
            if mk in low:
                return ats
    return None


def is_career_link(text, href):
    hay = " ".join([(text or "").lower(), (href or "").lower()])
    for kw in CAREER_KEYWORDS:
        if re.search(kw, hay):
            return True
    return False


def is_career_page(url):
    """Common direct career paths that are worth probing."""
    if not url:
        return False
    path = urlparse(url).path.lower().rstrip("/")
    for seg in ["/careers", "/career", "/jobs", "/join-us", "/joinus", "/jobs/careers",
                "/careers/jobs", "/job-openings", "/work-with-us", "/vacancies",
                "/career/jobs", "/karriere", "/stellenangebote", "/career-opportunities",
                "/join-our-team", "/careers-at", "/about/careers", "/recruiting",
                "/joblist", "/jobs2", "/job", "/stellesuche", "/trabaja", "/empleo"]:
        if path == seg or path.startswith(seg + "/"):
            return True
    return False


_MULTI_TLD = {"co", "com", "org", "net", "gov", "ac", "edu", "mil", "gen"}


def registrable_domain(host):
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in _MULTI_TLD and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def find_career_links(soup, base_url, limit=10):
    """Return official-domain or recognized ATS career links from a homepage."""
    found = []
    seen = set()
    if soup:
        base_host = urlparse(ensure_https(base_url)).hostname or ""
        base_dom = registrable_domain(base_host)
        anchors = soup.find_all("a", href=True)
        for a in anchors:
            text = " ".join(a.get_text(" ", strip=True).split())
            href = a["href"]
            full = url_join(base_url, href)
            if not full:
                continue
            external_ats, _ = detect_ats_in_url(full)
            if not is_career_link(text, href) and not external_ats:
                continue
            if EXCLUDE_PATH_RE.search(full):
                continue
            full_host = urlparse(full).hostname or ""
            same_domain = not base_dom or not full_host or registrable_domain(full_host) == base_dom
            if not same_domain and not external_ats:
                continue
            key = full.split("#")[0]
            if key in seen:
                continue
            seen.add(key)
            found.append((text, key))
    # Prioritize links whose text/href is strongly a careers/jobs page.
    def score(item):
        t, u = item
        s = 0
        if "career" in t or "career" in u:
            s += 3
        if "job" in t or "job" in u:
            s += 2
        if "join" in t or "work" in t:
            s += 1
        return -s

    found.sort(key=score)
    return [u for _, u in found[:limit]]


def validate_career_page(url, html_text, company_home_url=""):
    """Validate a career candidate using domain ownership and page evidence."""
    if not url or not html_text:
        return False
    candidate_host = urlparse(ensure_https(url)).hostname or ""
    company_host = urlparse(ensure_https(company_home_url)).hostname or ""
    same_domain = bool(candidate_host and company_host and
                       registrable_domain(candidate_host) == registrable_domain(company_host))
    ats, _ = detect_ats_in_url(url)
    soup = BeautifulSoup(html_text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    headings = " ".join(tag.get_text(" ", strip=True) for tag in soup.find_all(["h1", "h2", "h3"]))
    page_signal = is_career_link(title + " " + headings, url)
    jobposting_signal = bool(re.search(r'[@\"\']type[\"\']?\s*:\s*[\"\']JobPosting', html_text, re.I))
    job_link_signal = any(is_career_link(a.get_text(" ", strip=True), a.get("href", ""))
                          for a in soup.find_all("a", href=True)[:500])
    return bool((same_domain or ats) and (ats or page_signal or jobposting_signal or job_link_signal))


def common_career_urls(base_url):
    """Probe common career paths directly."""
    host = urlparse(base_url).netloc or urlparse(base_url).path
    scheme = urlparse(base_url).scheme or "https"
    base = "%s://%s" % (scheme, host)
    paths = [
        "/careers", "/career", "/jobs", "/jobs/careers", "/careers/jobs",
        "/join-us", "/career-opportunities", "/work-with-us", "/vacancies",
        "/join-our-team", "/about/careers", "/career/jobs", "/karriere",
        "/stellenangebote", "/job-openings", "/careers-at", "/recruiting",
    ]
    out = []
    for p in paths:
        out.append((base + p, 0))
    return out
