import json
import re

from bs4 import BeautifulSoup

from .fields import (
    clean_text,
    join_list,
    parse_date,
    parse_decimal,
    currency_from_salary,
    to_list,
    extract_labeled_fields,
    salary_breakdown,
)


def _walk(obj):
    if isinstance(obj, list):
        for item in obj:
            yield from _walk(item)
    elif isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)


def _type_name(obj):
    t = obj.get("@type")
    if isinstance(t, list):
        return t
    return [t] if t else []


def _is_jobposting(obj):
    names = _type_name(obj)
    return "JobPosting" in names


def _ld_scripts(soup):
    out = []
    for s in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        raw = s.string or s.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            out.append(data)
        except Exception:
            try:
                data = json.loads(raw.replace("\n", "").replace("\r", ""))
                out.append(data)
            except Exception:
                # try to salvage a JSON object inside a JS variable
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if m:
                    try:
                        out.append(json.loads(m.group(0)))
                    except Exception:
                        pass
    return out


def _text(value):
    if isinstance(value, dict):
        return clean_text(value.get("@value") or value.get("name") or value.get("text") or "")
    if isinstance(value, list):
        return " ".join(_text(v) for v in value)
    return clean_text(value)


def _extract_min_max(salary_obj):
    """Return (display, min_s, max_s, currency) from a baseSalary object."""
    display = ""
    mn = ""
    mx = ""
    cur = ""
    if isinstance(salary_obj, list):
        salary_obj = salary_obj[0] if salary_obj else {}
    if not isinstance(salary_obj, dict):
        txt = clean_text(salary_obj)
        if txt:
            return txt, "", "", currency_from_salary(txt)
        return "", "", "", ""

    value = salary_obj.get("value") or {}
    if isinstance(value, dict):
        vtype = value.get("@type", "")
        mn_raw = value.get("minValue") or value.get("min") or value.get("value")
        mx_raw = value.get("maxValue") or value.get("max") or value.get("value")
        mn = parse_decimal(mn_raw)
        mx = parse_decimal(mx_raw)
        if not mn and not mx:
            mn = parse_decimal(value.get("value"))
            mx = ""
        cur = clean_text(value.get("currency") or salary_obj.get("currency"))
        if mn and mx and mn != mx:
            display = "%s - %s" % (mn, mx)
        elif mn or mx:
            display = mn or mx
        else:
            unit = clean_text(value.get("unitText"))
            per = clean_text(value.get("unitText") or salary_obj.get("per"))
            display = clean_text(salary_obj.get("description"))
            if not display:
                display = ""
    else:
        txt = clean_text(value)
        if txt:
            display = txt
            mn = parse_decimal(re.search(r"[-–]?[\d,]+\.?\d*", txt.replace(" - ", "-")).group(0)) if re.search(r"[-–]?[\d,]+\.?\d*", txt.replace(" - ", "-")) else ""
            cur = currency_from_salary(txt)
    if not cur:
        cur = currency_from_salary(salary_obj.get("description"))
    return display, mn, mx, cur


def _extract_education(req):
    """Extract education_stream / type / qualification from educationRequirements."""
    stream = ""
    etype = ""
    qual = ""
    if isinstance(req, dict):
        name = _text(req.get("name"))
        if req.get("@type") in ("EducationalOccupationalCredential", "EducationalOccupationalProgram"):
            if name:
                qual = name
        # list of recognized degree types
        qual_types = ["phd", "doctorate", "master", "bachelor", "bachelors", "associate",
                      "diploma", "high school", "highschool", "matric", "12th", "degree",
                      "graduation", "post graduate", "postgraduate", "undergraduate"]
        if name and any(q in name.lower() for q in qual_types):
            qual = name
        if name and not qual:
            stream = name
        inner = req.get("credentialCategory") or req.get("educationalOccupationalCredential")
        if inner:
            qual = _text(inner)
    elif isinstance(req, list):
        parts = [_extract_education(x) for x in req]
        stream = "; ".join(p[0] for p in parts if p[0])
        etype = "; ".join(p[1] for p in parts if p[1])
        qual = "; ".join(p[2] for p in parts if p[2])
    else:
        txt = clean_text(req)
        if txt:
            qual_types = ["phd", "doctorate", "master", "bachelor", "diploma", "high school",
                          "associate", "degree", "graduation", "qualification"]
            if any(q in txt.lower() for q in qual_types):
                qual = txt
            else:
                stream = txt
    return stream, etype, qual


def _extract_skills(entry):
    """Collect skills from skills, knowsAbout, qualifications, description keywords."""
    skills = set()
    for key in ("skills", "knowsAbout", "about"):
        v = entry.get(key)
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    name = _text(item.get("name"))
                    if name:
                        skills.add(name)
                else:
                    name = clean_text(item)
                    if name:
                        skills.add(name)
        elif isinstance(v, dict):
            name = _text(v.get("name"))
            if name:
                skills.add(name)
    quals = entry.get("qualifications")
    if quals:
        txt = _text(quals)
        skills.add(txt)
    return join_list(sorted(skills))


def parse_jsonld_jobs(html_text, base_url=""):
    """Parse all JobPosting entries found in a page's JSON-LD."""
    jobs = []
    if not html_text:
        return jobs
    soup = BeautifulSoup(html_text, "html.parser")
    for data in _ld_scripts(soup):
        for obj in _walk(data):
            if not _is_jobposting(obj):
                continue
            job = {}
            job["job_title"] = _text(obj.get("title") or obj.get("name"))
            job["job_url"] = clean_text(obj.get("url"))
            job["posted_date"] = parse_date(obj.get("datePosted"))
            job["closed_date"] = parse_date(obj.get("validThrough"))
            emp = obj.get("employmentType")
            if isinstance(emp, list):
                emp = ", ".join(clean_text(e) for e in emp)
            job["employment_type"] = clean_text(emp)
            desc = _text(obj.get("description"))
            job["job_description"] = desc
            job["job_status"] = "Active"
            # salary
            disp, mn, mx, cur = _extract_min_max(obj.get("baseSalary"))
            job["salary"] = disp
            job["min_salary"] = mn
            job["max_salary"] = mx
            job["currency"] = cur
            # education
            ed = obj.get("educationRequirements")
            stream, etype, qual = _extract_education(ed)
            job["education_stream"] = stream
            job["education_type"] = etype
            job["education_qualification"] = qual
            # experience
            exp = obj.get("experienceRequirements")
            if isinstance(exp, dict):
                mon = exp.get("monthsOfExperience") or exp.get("yearsOfExperience")
                if mon:
                    job["years_of_experience"] = clean_text(mon)
                else:
                    job["years_of_experience"] = _text(exp)
            elif exp:
                job["years_of_experience"] = _text(exp)
            else:
                job["years_of_experience"] = ""
            # seniority (not standard in schema)
            job["seniority_level"] = ""
            # skills
            job["skills"] = _extract_skills(obj)
            # hiring org
            ho = obj.get("hiringOrganization")
            if isinstance(ho, dict):
                job["company_name"] = _text(ho.get("name"))
            else:
                job["company_name"] = ""
            job["source"] = "jsonld"
            jobs.append(job)
    return jobs


# ---------------------------------------------------------------------------
# Microdata (schema.org itemscope/itemprop) parsing
# ---------------------------------------------------------------------------
_ITEMPROP_TEXT_TAGS = ("meta", "link")


def _scope_props(root):
    """Gather {prop: [values]} for direct itemprops of a scope, recursing into
    nested scopes only for selected properties (e.g. baseSalary, location)."""
    props = {}
    nested = {}
    for el in root.find_all(recursive=True):
        if el is root:
            continue
        if el.has_attr("itemscope"):
            if el is not root and el.find_parent(attrs={"itemscope": True}) is not root:
                continue
            itype = (el.get("itemtype") or "").lower()
            for p in ("baseSalary", "jobLocation", "educationRequirements",
                      "experienceRequirements", "hiringOrganization", "address"):
                if ("schema.org/" + p).lower() in itype:
                    nested[p] = el
            continue
        if not el.has_attr("itemprop"):
            continue
        prop = el.get("itemprop")
        if isinstance(prop, list):
            prop = prop[0]
        val = ""
        if el.name == "meta":
            val = el.get("content") or ""
        elif el.name == "link":
            val = el.get("href") or ""
        elif el.name in ("img",):
            val = el.get("alt") or ""
        else:
            val = el.get_text(" ", strip=True)
        props.setdefault(prop, []).append(clean_text(val))
    return props, nested


def _microdata_job(scope):
    props, nested = _scope_props(scope)
    job = {}
    title = props.get("title") or props.get("name")
    job["job_title"] = title[0] if title else ""
    job["job_url"] = props.get("url", [""])[0]
    job["posted_date"] = parse_date(props.get("datePosted", [""])[0])
    job["closed_date"] = parse_date(props.get("validThrough", [""])[0])
    job["employment_type"] = ", ".join(props.get("employmentType", []))
    job["job_description"] = props.get("description", [""])[0]
    job["job_status"] = "Active"
    job["salary"] = ""
    job["min_salary"] = ""
    job["max_salary"] = ""
    job["currency"] = ""
    loc = nested.get("jobLocation")
    if loc is not None:
        lp, lnested = _scope_props(loc)
        parts = []
        for k in ("addressLocality", "addressRegion", "postalCode", "addressCountry", "streetAddress"):
            if lp.get(k):
                parts.append(lp[k][0])
        if parts:
            job["location"] = ", ".join(x for x in parts if x)
    ho = nested.get("hiringOrganization")
    if ho is not None:
        hp, _ = _scope_props(ho)
        job["company_name"] = (hp.get("name") or [""])[0]
    sal = nested.get("baseSalary")
    if sal is not None:
        sp, _ = _scope_props(sal)
        mn = parse_decimal(sp.get("minValue", [""])[0] or sp.get("value", [""])[0])
        mx = parse_decimal(sp.get("maxValue", [""])[0] or sp.get("value", [""])[0])
        cur = clean_text(sp.get("currency", [""])[0])
        job["currency"] = cur
        job["min_salary"] = mn
        job["max_salary"] = mx
        if mn and mx and mn != mx:
            job["salary"] = "%s - %s" % (mn, mx)
        elif mn or mx:
            job["salary"] = mn or mx
    ed = nested.get("educationRequirements")
    if ed is not None:
        ep, _ = _scope_props(ed)
        qual = ", ".join(ep.get("credentialCategory", []) + ep.get("name", []))
        job["education_qualification"] = qual
    elif props.get("educationRequirements"):
        job["education_qualification"] = ", ".join(props["educationRequirements"])
    exp = nested.get("experienceRequirements")
    if exp is not None:
        xp, _ = _scope_props(exp)
        job["years_of_experience"] = ", ".join(
            xp.get("monthsOfExperience", []) + xp.get("yearsOfExperience", [])
        )
    elif props.get("experienceRequirements"):
        job["years_of_experience"] = ", ".join(props["experienceRequirements"])
    job["education_stream"] = ""
    job["education_type"] = ""
    job["seniority_level"] = ""
    job["skills"] = ""
    job["source"] = "microdata"
    # enrich with explicitly-labeled fields present in the description
    labeled = extract_labeled_fields(job.get("job_description") or "")
    for key in ("employment_type", "seniority_level", "education_qualification",
                "years_of_experience", "salary"):
        if not job.get(key) and labeled.get(key):
            job[key] = labeled[key]
    if job.get("salary"):
        disp, mn, mx = salary_breakdown(job["salary"])
        if mn:
            job["min_salary"] = mn
        if mx:
            job["max_salary"] = mx
        if not job.get("currency"):
            job["currency"] = currency_from_salary(job["salary"])
    return job


def parse_microdata_jobs(html_text, base_url=""):
    """Parse schema.org JobPosting microdata blocks."""
    jobs = []
    if not html_text:
        return jobs
    soup = BeautifulSoup(html_text, "html.parser")
    for scope in soup.find_all(attrs={"itemscope": True, "itemtype": True}):
        itype = scope.get("itemtype")
        if isinstance(itype, list):
            itype = " ".join(itype)
        if "jobposting" not in (itype or "").lower():
            continue
        job = _microdata_job(scope)
        if job["job_title"] or job["job_url"] or job["job_description"]:
            if not job["job_url"] and base_url:
                job["job_url"] = base_url
            jobs.append(job)
    return jobs
