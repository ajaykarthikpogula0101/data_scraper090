import re
import json
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .fields import (
    clean_text,
    join_list,
    parse_date,
    parse_decimal,
    currency_from_salary,
    to_list,
    empty_job,
    extract_labeled_fields,
)
from .urlutils import ensure_https, url_join
from .parsers_jsonld import parse_jsonld_jobs, parse_microdata_jobs


def make_job(ats_name, title="", url="", source=""):
    j = empty_job()
    j["job_title"] = title
    j["job_url"] = url
    j["job_status"] = "Active"
    j["source"] = source or ats_name
    return j


def _strip_html(text):
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return clean_text(soup.get_text(" ", strip=True))


def _meta_to_dict(metadata_list):
    d = {}
    if not metadata_list:
        return d
    for m in metadata_list:
        if isinstance(m, dict):
            key = clean_text(m.get("name") or m.get("id"))
            val = m.get("value")
            if isinstance(val, (dict, list)):
                val = clean_text(val.get("name") or val) if isinstance(val, dict) else clean_text(val)
            d[key.lower()] = clean_text(val)
    return d


def _lookup_employment(d, keys=("work type", "employment type", "type", "job type", "emp-type")):
    for k in keys:
        if k in d:
            return d[k]
    return ""


def _location_text(value):
    if isinstance(value, list):
        return "; ".join(filter(None, (_location_text(item) for item in value)))
    if isinstance(value, dict):
        parts = []
        for key in ("name", "fullLocation", "location", "city", "region", "state", "country"):
            text = clean_text(value.get(key))
            if text and text not in parts:
                parts.append(text)
        return ", ".join(parts)
    return clean_text(value)


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------
def parse_greenhouse(session, info):
    board = info.get("board") or ""
    api = "https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true" % board
    data = session.fetch_json(api, timeout=30)
    if not data or not isinstance(data, dict):
        return []
    jobs = []
    for item in data.get("jobs", []):
        if not isinstance(item, dict):
            continue
        j = make_job("greenhouse", item.get("title"), item.get("absolute_url"))
        j["posted_date"] = parse_date(item.get("updated_at") or item.get("first_published"))
        j["job_description"] = _strip_html(item.get("content"))
        loc = item.get("location") or {}
        if isinstance(loc, dict):
            j["job_location"] = clean_text(loc.get("name"))
        meta = _meta_to_dict(item.get("metadata"))
        j["employment_type"] = _lookup_employment(meta)
        for d in item.get("departments") or []:
            pass
        jobs.append(j)
    return jobs


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------
def parse_lever(session, info):
    slug = info.get("board") or info.get("slug") or ""
    api = "https://api.lever.co/v0/postings/%s?mode=json" % slug
    data = session.fetch_json(api, timeout=30)
    if not isinstance(data, list):
        return []
    jobs = []
    for item in data:
        if not isinstance(item, dict):
            continue
        j = make_job("lever", item.get("text"), item.get("hostedUrl"))
        j["posted_date"] = parse_date(item.get("createdAt"))
        cats = item.get("categories") or {}
        j["employment_type"] = clean_text(cats.get("commitment"))
        j["job_location"] = _location_text(cats.get("location") or item.get("workplaceType") or
                                             item.get("allLocations"))
        j["job_description"] = clean_text(item.get("descriptionPlain") or item.get("description"))
        j["salary"] = clean_text(item.get("salaryRange"))
        j["source"] = "lever"
        jobs.append(j)
    return jobs
# ---------------------------------------------------------------------------
# SmartRecruiters
# ---------------------------------------------------------------------------
def _sr_label(value):
    if isinstance(value, dict):
        return clean_text(value.get("label") or value.get("name"))
    return clean_text(value)


def parse_smartrecruiters(session, info):
    slug = info.get("board") or ""
    jobs = []
    offset = 0
    while offset < 500:
        api = "https://api.smartrecruiters.com/v1/companies/%s/postings?limit=100&offset=%d" % (slug, offset)
        data = session.fetch_json(api, timeout=30)
        if not data:
            break
        content = data.get("content") or []
        if not content:
            break
        for item in content:
            if not isinstance(item, dict):
                continue
            status = (item.get("postingStatus") or {})
            if isinstance(status, dict):
                st = clean_text(status.get("status"))
            else:
                st = clean_text(status)
            if st and st.lower() not in ("active", "published", ""):
                continue
            j = make_job("smartrecruiters", item.get("name"))
            j["posted_date"] = parse_date(item.get("releasedDate") or item.get("createdAt"))
            j["employment_type"] = _sr_label(item.get("typeOfEmployment") or item.get("employmentType"))
            j["seniority_level"] = _sr_label(item.get("experienceLevel"))
            loc = item.get("location")
            if isinstance(loc, dict):
                j["job_location"] = clean_text(loc.get("fullLocation") or loc.get("city"))
            j["job_url"] = clean_text(item.get("postingUrl")) or (
                "https://jobs.smartrecruiters.com/%s/%s" % (slug, item.get("id")))
            jobs.append(j)
        total = data.get("totalFound") or 0
        offset += len(content)
        if offset >= total:
            break
    # enrich descriptions from detail endpoint (capped)
    for j in jobs[:30]:
        pid = (j.get("job_url") or "").split("/")[-1]
        if not pid or not str(pid).isdigit():
            continue
        detail = session.fetch_json(
            "https://api.smartrecruiters.com/v1/companies/%s/postings/%s" % (slug, pid),
            timeout=25,
        )
        if not detail:
            continue
        ja = detail.get("jobAd")
        if isinstance(ja, dict):
            sections = ja.get("sections") or {}
            if isinstance(sections, dict):
                sections = list(sections.values())
            for sec in sections:
                if not isinstance(sec, dict):
                    continue
                name = clean_text(sec.get("sectionName") or sec.get("title"))
                body = clean_text(sec.get("description") or sec.get("text"))
                if name.lower() in (
                    "jobdescription", "job description", "description", "the role",
                    "about the job", "your role", "what we offer", "responsibilities",
                    "requirements", "qualifications", "skills", "your profile", "about you",
                ):
                    if body and not j["job_description"]:
                        j["job_description"] = body
                elif "skills" in name.lower() and body and not j["skills"]:
                    j["skills"] = body
        if not j["job_url"] and detail.get("postingUrl"):
            j["job_url"] = clean_text(detail.get("postingUrl"))
    return jobs


# ---------------------------------------------------------------------------
# Workable
# ---------------------------------------------------------------------------
def parse_workable(session, info):
    slug = info.get("board") or ""
    api = "https://apply.workable.com/api/v1/widget/accounts/%s?details=true" % slug
    data = session.fetch_json(api, timeout=30)
    if not data:
        return []
    jobs = []
    for item in data.get("jobs", []):
        if not isinstance(item, dict):
            continue
        j = make_job("workable", item.get("title"), item.get("url") or item.get("shortlink"))
        j["posted_date"] = parse_date(item.get("published_on") or item.get("created_at") or item.get("created"))
        j["employment_type"] = clean_text(item.get("employment_type"))
        j["job_location"] = _location_text(item.get("location") or item.get("locations") or
                                             item.get("office"))
        j["seniority_level"] = clean_text(item.get("seniority"))
        j["job_description"] = _strip_html(item.get("description"))
        j["salary"] = clean_text(item.get("salary"))
        if not j["job_url"]:
            j["job_url"] = "https://apply.workable.com/%s/j/%s" % (slug, item.get("shortcode"))
        jobs.append(j)
    return jobs


# ---------------------------------------------------------------------------
# Recruitee
# ---------------------------------------------------------------------------
def parse_recruitee(session, info):
    slug = info.get("board") or ""
    api = "https://%s.recruitee.com/api/offers/" % slug
    data = session.fetch_json(api, timeout=30)
    if not data:
        return []
    jobs = []
    for item in data.get("offers", []):
        if not isinstance(item, dict):
            continue
        j = make_job("recruitee", item.get("title"))
        j["posted_date"] = parse_date(item.get("created_at") or item.get("published_at"))
        j["employment_type"] = clean_text(item.get("employment_type"))
        j["job_location"] = _location_text(item.get("location") or item.get("locations") or
                                             item.get("city") or item.get("country"))
        j["job_description"] = _strip_html(item.get("description"))
        slug_name = item.get("slug")
        j["job_url"] = "https://%s.recruitee.com/o/%s" % (slug, slug_name or item.get("id"))
        jobs.append(j)
    return jobs


# ---------------------------------------------------------------------------
# Breezy
# ---------------------------------------------------------------------------
def parse_breezy(session, info):
    slug = info.get("board") or ""
    jobs = []
    for cand in ("https://%s.breezy.hr/positions.json" % slug,
                 "https://%s.breezy.hr/json" % slug,
                 "https://%s.breezy.hr/api/positions" % slug):
        data = session.fetch_json(cand, timeout=30)
        if isinstance(data, dict):
            data = data.get("positions") or data.get("data") or []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                j = make_job("breezy", item.get("name") or item.get("title"),
                             item.get("public_url") or item.get("url"))
                j["posted_date"] = parse_date(item.get("created_at") or item.get("published_at"))
                j["job_description"] = _strip_html(item.get("description"))
                j["employment_type"] = clean_text(item.get("type") or item.get("employment_type"))
                j["job_location"] = _location_text(item.get("location") or item.get("locations"))
                j["job_url"] = j["job_url"] or "https://%s.breezy.hr/p/%s" % (slug, item.get("slug"))
                jobs.append(j)
            if jobs:
                break
    return jobs


# ---------------------------------------------------------------------------
# JazzHR
# ---------------------------------------------------------------------------
def parse_jazzhr(session, info):
    slug = info.get("board") or ""
    jobs = []
    # public JSON endpoint
    for cand in ("https://%s.applytojob.com/api/jobs" % slug,
                 "https://%s.jazzhr.com/api/jobs" % slug,
                 "https://%s.applytojob.com/apply/jobs" % slug):
        data = session.fetch_json(cand, timeout=30)
        items = None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("jobs") or data.get("data") or data.get("results")
        if items:
            for item in items:
                if not isinstance(item, dict):
                    continue
                j = make_job("jazzhr", item.get("title"))
                j["posted_date"] = parse_date(item.get("date_created") or item.get("created_at") or item.get("posted_on"))
                j["job_description"] = _strip_html(item.get("description") or item.get("job_description"))
                j["job_url"] = clean_text(item.get("apply_url") or item.get("url"))
                j["salary"] = clean_text(item.get("salary") or item.get("pay_range"))
                j["job_location"] = _location_text(item.get("location") or item.get("locations") or
                                                     item.get("city"))
                jobs.append(j)
            break
    return jobs


# ---------------------------------------------------------------------------
# Personio (public XML feed; search.json fallback)
# ---------------------------------------------------------------------------
import xml.etree.ElementTree as ET


def parse_personio(session, info):
    slug = info.get("board") or ""
    tld = info.get("tld") or "de"
    base = "https://%s.jobs.personio.%s" % (slug, tld)
    jobs = []

    xml_text = session.fetch_text(base + "/xml", timeout=35)
    if xml_text:
        try:
            root = ET.fromstring(xml_text)
            if root.tag == "workzag-jobs":
                for p in root.findall("position"):
                    j = _personio_xml_item(p, slug, tld)
                    if j:
                        jobs.append(j)
                if jobs:
                    return jobs
        except ET.ParseError:
            pass

    data = session.fetch_json(base + "/search.json", timeout=30)
    if isinstance(data, list):
        for item in data:
            j = _personio_item(item, slug, base)
            if j:
                jobs.append(j)
    elif isinstance(data, dict) and data.get("data"):
        for item in data["data"]:
            j = _personio_item(item, slug, base)
            if j:
                jobs.append(j)
    return jobs


def _personio_xml_item(p, slug, tld):
    def tx(tag):
        el = p.find(tag)
        return clean_text(el.text) if el is not None and el.text else ""

    j = make_job("personio", tx("name"))
    pid = tx("id")
    j["job_url"] = "https://%s.jobs.personio.%s/job/%s" % (slug, tld, pid)
    j["posted_date"] = parse_date(tx("createdAt"))
    offices = [tx("office")]
    add = p.find("additionalOffices")
    if add is not None:
        for o in add.findall("office"):
            if o.text:
                offices.append(clean_text(o.text))
    j["job_location"] = ", ".join(x for x in offices if x)
    j["employment_type"] = tx("schedule") or tx("employmentType")
    if tx("employmentType").lower() == "intern":
        j["employment_type"] = "Internship"
    j["seniority_level"] = tx("seniority")
    j["years_of_experience"] = tx("yearsOfExperience")
    j["skills"] = tx("keywords")
    desc_parts = []
    for group_tag in ("jobDescriptions", "jobRequirements"):
        grp = p.find(group_tag)
        if grp is None:
            continue
        for sec in grp.findall("jobDescription"):
            name = clean_text(sec.findtext("name"))
            raw = sec.findtext("value") or "".join(sec.itertext())
            val = _strip_html(raw)
            if name and val:
                desc_parts.append("%s: %s" % (name, val))
            elif val:
                desc_parts.append(val)
    j["job_description"] = "\n\n".join(desc_parts)
    j["salary"] = tx("salaryInformation")
    return j


def _personio_item(item, slug, base):
    if not isinstance(item, dict):
        return None
    j = make_job("personio", item.get("name") or item.get("jobName"))
    pid = item.get("id") or item.get("jobId")
    j["job_url"] = "https://%s.jobs.personio.%s/job/%s" % (slug, base.split("personio.")[1], pid)
    j["posted_date"] = parse_date(item.get("publishedAt") or item.get("publishedAt"))
    j["employment_type"] = clean_text(item.get("employmentType") or item.get("workType") or item.get("schedule"))
    j["job_description"] = clean_text(item.get("description") or item.get("jobDescription"))
    j["job_location"] = _location_text(item.get("location") or item.get("workplace"))
    j["seniority_level"] = clean_text(item.get("seniorityLevel") or item.get("careerLevel"))
    j["salary"] = clean_text(item.get("salary") or item.get("salaryInformation"))
    return j


# ---------------------------------------------------------------------------
# Workday (cxs JSON endpoint)
# ---------------------------------------------------------------------------
def parse_workday(session, info):
    base = info.get("base_url") or ""
    tenant = info.get("tenant") or ""
    domain = info.get("domain") or tenant
    if not base or not tenant:
        return []
    jobs = []
    offset = 0
    while offset < 500:
        api = "%s/wday/cxs/%s/%s/jobs" % (base, tenant, domain)
        payload = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}
        data = session.post_json(api, payload, timeout=35)
        if not isinstance(data, dict):
            break
        items = data.get("jobPostings") or []
        if not items:
            break
        for it in items:
            if not isinstance(it, dict):
                continue
            j = make_job("workday", it.get("title"))
            ext = it.get("externalPath") or ""
            j["job_url"] = (base + ext) if ext.startswith("/") else (base + "/" + ext if ext else base)
            j["posted_date"] = parse_date(it.get("postedOn") or it.get("postedOnDateTime"))
            j["employment_type"] = clean_text(it.get("jobRequisitionType") or it.get("workerType") or it.get("timeType"))
            locs = it.get("locationsText") or it.get("locations") or ""
            j["job_location"] = _location_text(locs)
            j["salary"] = clean_text(it.get("compensation") or it.get("payRate"))
            jobs.append(j)
        total = data.get("total") or 0
        offset += len(items)
        if offset >= total:
            break
    return jobs


def workday_info_from_url(url):
    """Extract tenant/domain/base from a myworkdayjobs URL."""
    p = urlparse(url)
    base = "%s://%s" % (p.scheme, p.netloc)
    segs = [s for s in p.path.split("/") if s]
    tenant = ""
    domain = ""
    if "wday" in segs:
        i = segs.index("wday")
        if i + 1 < len(segs):
            tenant = segs[i + 1]
        if i + 2 < len(segs):
            domain = segs[i + 2]
    elif segs:
        tenant = segs[0]
        domain = segs[0]
    if not tenant:
        host = p.netloc.split(".")[0]
        tenant = host
        domain = host
    return {"base_url": base, "tenant": tenant, "domain": domain}


# ---------------------------------------------------------------------------
# SuccessFactors (classic jobsearch endpoint + microdata detail pages)
# ---------------------------------------------------------------------------
def parse_successfactors(session, info):
    base = info.get("base_url") or ""
    if not base:
        return []
    # resolve redirects to find the real SuccessFactors host
    r = session.fetch(base, timeout=30)
    final = r.url if (r is not None and r.url) else base
    host = urlparse(final).netloc or urlparse(base).netloc
    job_links = []
    seen = set()
    for start in range(0, 200, 50):
        search = "https://%s/jobsearch/search?q=&location=&num=50&start=%d" % (host, start)
        html = session.fetch_text(search, timeout=35)
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        found = False
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r"/job/", href):
                full = url_join("https://%s/" % host, href)
                if full and full not in seen:
                    seen.add(full)
                    job_links.append(full)
                    found = True
        if not found:
            break
    jobs = []
    for u in job_links[:60]:
        detail = session.fetch_text(u, timeout=30)
        if not detail:
            continue
        djobs = parse_jsonld_jobs(detail, u)
        if not djobs:
            djobs = parse_microdata_jobs(detail, u)
        if djobs:
            for j in djobs:
                if not j["job_url"]:
                    j["job_url"] = u
            jobs.extend(djobs)
            continue
        # fallback micro-parse
        dsoup = BeautifulSoup(detail, "html.parser")
        j = make_job("successfactors", "")
        og = dsoup.find("meta", attrs={"property": "og:title"})
        j["job_title"] = clean_text(og.get("content")) if og else ""
        date_meta = dsoup.find("meta", attrs={"itemprop": "datePosted"})
        if date_meta:
            j["posted_date"] = parse_date(date_meta.get("content"))
        disp = dsoup.find("div", class_=re.compile("jobDisplay"))
        if disp:
            j["job_description"] = clean_text(disp.get_text(" ", strip=True))
        j["job_url"] = u
        if j["job_title"] or j["job_description"]:
            labeled = extract_labeled_fields(j["job_description"])
            for key in ("employment_type", "seniority_level", "education_qualification",
                        "years_of_experience", "salary"):
                if not j.get(key) and labeled.get(key):
                    j[key] = labeled[key]
            jobs.append(j)
    return jobs
# ---------------------------------------------------------------------------
# BambooHR
# ---------------------------------------------------------------------------
def parse_bamboo(session, info):
    slug = info.get("board") or ""
    base = "https://%s.bamboohr.com/careers" % slug
    html = session.fetch_text(base, timeout=30)
    if not html:
        return []
    jobs = []
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"/careers/\d+", href):
            u = url_join(base, href)
            if u and u not in seen:
                seen.add(u)
    jsonld = parse_jsonld_jobs(html, base)
    if jsonld:
        jobs.extend(jsonld)
    for u in list(seen)[:20]:
        detail = session.fetch_text(u, timeout=25)
        if detail:
            jobs.extend(parse_jsonld_jobs(detail, u))
    return jobs
