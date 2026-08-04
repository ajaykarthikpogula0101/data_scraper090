import re
from datetime import datetime, timezone

_WS_RE = re.compile(r"\s+")


def clean_text(value, max_len=20000):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("\x00", "")
    value = _WS_RE.sub(" ", value).strip()
    if max_len and len(value) > max_len:
        value = value[:max_len].rstrip()
    return value


def to_list(value):
    """Normalize a value (string, list, comma separated) to a clean list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    elif isinstance(value, (int, float)):
        items = [str(value)]
    else:
        parts = str(value)
        parts = re.split(r"[,;\n|]", parts)
        items = parts
    out = []
    for it in items:
        it = clean_text(it, max_len=500)
        if it:
            out.append(it)
    return out


def join_list(value, sep="; "):
    return sep.join(to_list(value))


def parse_date(value):
    """Return an ISO date string (YYYY-MM-DD) from various formats, else ''."""
    if value is None:
        return ""
    if isinstance(value, (datetime,)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (int, float)):
        # epoch milliseconds or seconds
        try:
            if value > 10**11:
                value = value / 1000.0
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return ""
    text = clean_text(value, max_len=200)
    if not text:
        return ""
    # ISO / with time
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
        except Exception:
            return ""
    # dd/mm/yyyy or dd.mm.yyyy
    m = re.search(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", text)
    if m:
        try:
            return "%s-%02d-%02d" % (m.group(3), int(m.group(2)), int(m.group(1)))
        except Exception:
            return ""
    # mm/dd/yyyy
    m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})", text)
    if m:
        try:
            return "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2)))
        except Exception:
            return ""
    # Mon DD, YYYY
    m = re.search(
        r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", text
    )
    if m:
        months = {
            "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
            "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
            "november": 11, "december": 12,
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
            "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        mon = months.get(m.group(1).lower()[:3])
        if mon:
            try:
                return "%s-%02d-%02d" % (m.group(3), mon, int(m.group(2)))
            except Exception:
                return ""
    # Mon DD HH:MM:SS TZ YYYY   (SuccessFactors datePosted)
    m = re.search(
        r"([A-Za-z]{3})\s+(\d{1,2})\s+\d{1,2}:\d{2}:\d{2}\s+[A-Z]{2,4}\s+(\d{4})", text
    )
    if m:
        months = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        mon = months.get(m.group(1).lower()[:3])
        if mon:
            try:
                return "%s-%02d-%02d" % (m.group(3), mon, int(m.group(2)))
            except Exception:
                return ""
    return ""


def parse_decimal(value):
    text = clean_text(value, max_len=50)
    if not text:
        return ""
    m = re.search(r"-?\d[\d,]*\.?\d*", text.replace("\u00a0", " "))
    if not m:
        return ""
    num = m.group(0).replace(",", "")
    try:
        f = float(num)
        if f == int(f):
            return str(int(f))
        return str(f)
    except Exception:
        return ""


def currency_from_salary(value):
    text = clean_text(value, max_len=50)
    for cur in ["USD", "EUR", "GBP", "INR", "AUD", "CAD", "CHF", "JPY", "CNY",
                "SEK", "NOK", "DKK", "PLN", "BRL", "HKD", "SGD", "NZD", "ZAR",
                "CZK", "HUF", "MXN"]:
        if cur in text.upper():
            return cur
    m = re.search(r"[€$£¥₹]\s?", text)
    if m:
        return {"€": "EUR", "$": "USD", "£": "GBP", "¥": "JPY", "₹": "INR"}[m.group(0)[0]]
    return ""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_LABELED_RE = {
    "employment_type": re.compile(
        r"(?:employment\s*type|job\s*type|type\s*of\s*employment|work\s*type|"
        r"employment\s*tag|schedule)\s*[:\-]\s*([^|\n\r]+?)(?=\s*(?:career\s*status"
        r"|requisition|expected\s*travel|additional\s*locations|job\s*id|location"
        r"|salary|compensation|education|qualification|experience|remote|#|$))",
        re.IGNORECASE,
    ),
    "seniority_level": re.compile(
        r"(?:career\s*status|seniority(?:\s*level)?|level|experience\s*level|"
        r"career\s*level)\s*[:\-]\s*([^|\n\r]+?)(?=\s*(?:requisition|expected\s*"
        r"travel|additional\s*locations|employment\s*type|job\s*id|location|#|$))",
        re.IGNORECASE,
    ),
    "education_qualification": re.compile(
        r"(?:education(?:al)?\s*(?:qualification|requirement|level|type)?|qualification|"
        r"degree\s*(?:requirement|level)?|minimum\s*qualification)\s*[:\-]\s*([^|\n\r]+?)"
        r"(?=\s*(?:requisition|experience|skills|job\s*id|location|#|$))",
        re.IGNORECASE,
    ),
    "years_of_experience": re.compile(
        r"(?:years?\s*of\s*experience|experience\s*(?:required)?|minimum\s*experience)"
        r"\s*[:\-]\s*([^|\n\r]+?)(?=\s*(?:requisition|education|skills|job\s*id|#|$))",
        re.IGNORECASE,
    ),
    "salary": re.compile(
        r"(?:salary|compensation|pay\s*range|annual\s*salary|pay)\s*[:\-]\s*([^|\n\r]+?)"
        r"(?=\s*(?:requisition|experience|education|job\s*id|#|$))",
        re.IGNORECASE,
    ),
}

_SALARY_CURRENCIES = ["usd", "eur", "gbp", "inr", "aud", "cad", "chf", "jpy", "cn", "sek",
                      "nok", "dkk", "pln", "brl", "$", "€", "£", "₹", "¥"]


def extract_labeled_fields(text):
    """Extract explicitly-labeled fields from a job description text.

    Only captures fields the posting itself states (e.g. 'Employment Type: Full Time').
    Returns a dict of column -> clean value.
    """
    out = {}
    if not text:
        return out
    for key, pat in _LABELED_RE.items():
        m = pat.search(text)
        if not m:
            continue
        val = m.group(1)
        val = re.split(r"(?:\r?\n|•|\|\||:)", val)[0]
        val = clean_text(val, max_len=300)
        val = val.rstrip(",;")
        if len(val) < 2:
            continue
        if key == "years_of_experience" and not re.search(r"\d", val):
            continue
        if key == "salary" and not re.search(r"\d|€|$|£|₹|¥", val):
            continue
        out[key] = val
    return out


def salary_breakdown(salary_text):
    """Split a salary text into display, min, max."""
    if not salary_text:
        return "", "", ""
    text = clean_text(salary_text, max_len=200)
    numbers = [parse_decimal(x) for x in re.findall(r"-?\d[\d,]*\.?\d*\s*-\s*-?\d[\d,]*\.?\d*|[-–]?\d[\d,]*\.?\d*", text.replace("\u2013", "-").replace("\u2014", "-"))]
    nums = [n for n in numbers if n]
    mn = nums[0] if len(nums) >= 1 else ""
    mx = nums[1] if len(nums) >= 2 else ""
    if mn and mx and mn != mx:
        return "%s - %s" % (mn, mx), mn, mx
    if mn:
        return mn, mn, ""
    return text, "", ""


def empty_job():
    return {
        "job_title": "",
        "posted_date": "",
        "closed_date": "",
        "job_status": "",
        "last_checked_at": "",
        "education_stream": "",
        "education_type": "",
        "education_qualification": "",
        "years_of_experience": "",
        "years_of_experience_min": "",
        "years_of_experience_max": "",
        "seniority_level": "",
        "employment_type": "",
        "skills": "",
        "description_language": "",
        "job_description": "",
        "job_description_clean": "",
        "job_url": "",
        "salary_disclosed": "",
        "salary": "",
        "min_salary": "",
        "max_salary": "",
        "currency": "",
        "source": "",
    }
