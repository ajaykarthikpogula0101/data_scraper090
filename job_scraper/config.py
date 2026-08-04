import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INPUT_FILE = os.path.join(BASE_DIR, "COMPANYWEB29th_July.xlsx")
OUTPUT_FILE = os.path.join(BASE_DIR, "company_jobs_output.csv")
STATE_FILE = os.path.join(BASE_DIR, "scraper_state.json")
LOG_FILE = os.path.join(BASE_DIR, "scraper.log")

DEFAULT_TIMEOUT = 18
HOMEPAGE_TIMEOUT = 20
CAREER_TIMEOUT = 25
DEFAULT_WORKERS = 10
MAX_CAREER_LINKS = 5
MAX_JOB_DETAIL_PAGES = 20
MAX_JOBS_PER_COMPANY = 200

ENABLE_WEB_SEARCH = True
SEARCH_TIMEOUT = 12
SEARCH_MAX_RESULTS = 5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

OUTPUT_COLUMNS = [
    "company_name",
    "country",
    "website",
    "job_title",
    "posted_date",
    "closed_date",
    "job_status",
    "last_checked_at",
    "education_stream",
    "education_type",
    "education_qualification",
    "years_of_experience_min",
    "years_of_experience_max",
    "seniority_level",
    "employment_type",
    "skills",
    "description_language",
    "job_description",
    "job_description_clean",
    "job_url",
    "salary_disclosed",
    "salary",
    "min_salary",
    "max_salary",
    "currency",
    "source",
    "scraped_at",
]
