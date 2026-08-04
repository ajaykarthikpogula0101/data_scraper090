import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from job_scraper.clean_html import html_to_plain_text
from job_scraper.detect_language import detect_language
from job_scraper.llm_extract import ExtractionValidationError, validate_extraction
from job_scraper.parsers_jsonld import parse_jsonld_jobs
from job_scraper.recrawl_closed_check import classify_response, recrawl_csv
from job_scraper import pipeline
from job_scraper.company import _candidate_ats
from job_scraper.ats import find_career_links, validate_career_page
from job_scraper import websearch
from job_scraper.parsers_generic import _extract_listing_jobs, _parse_hrmanager_detail
from job_scraper.fields import empty_job
from bs4 import BeautifulSoup


class FakeResponse:
    def __init__(self, status_code=200, text="", url="https://example.test/job/1"):
        self.status_code = status_code
        self.text = text
        self.url = url


class FakeSession:
    def __init__(self, response):
        self.response = response

    def fetch(self, *args, **kwargs):
        return self.response


class DataQualityTests(unittest.TestCase):
    def test_jsonld_url_falls_back_to_fetched_page(self):
        payload = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Engineer"}
        html = '<script type="application/ld+json">%s</script>' % json.dumps(payload)
        jobs = parse_jsonld_jobs(html, base_url="https://example.test/jobs/123")
        self.assertEqual(jobs[0]["job_url"], "https://example.test/jobs/123")

    def test_jsonld_rejects_placeholder_fields(self):
        payload = {
            "@context": "https://schema.org", "@type": "JobPosting", "title": "Analyst",
            "educationRequirements": {"name": "UNAVAILABLE"},
            "qualifications": "<p>Full requirements paragraph, not a skill list</p>",
            "baseSalary": {"currency": "USD", "value": {"minValue": 0, "maxValue": 0}},
        }
        html = '<script type="application/ld+json">%s</script>' % json.dumps(payload)
        job = parse_jsonld_jobs(html, base_url="https://example.test/job")[0]
        self.assertEqual(job["education_stream"], "")
        self.assertEqual(job["skills"], "")
        self.assertEqual((job["salary"], job["min_salary"], job["max_salary"], job["currency"]),
                         ("", "", "", ""))

    def test_html_cleaning_decodes_entities_and_removes_tags(self):
        value = "<div>Hello &amp; welcome</div><script>bad()</script><p>Use &lt;tools&gt;</p>"
        cleaned = html_to_plain_text(value)
        self.assertEqual(cleaned, "Hello & welcome\nUse")
        self.assertNotRegex(cleaned, r"<[^>]+>")

    def test_language_detection_uses_clean_description(self):
        self.assertEqual(detect_language("This is a software engineering role requiring Python experience."), "en")

    def _valid_extraction(self, disclosed=False):
        return {
            "years_of_experience_min": 3,
            "years_of_experience_max": 5,
            "seniority_level": "Mid",
            "education_stream": "Computer Science",
            "education_type": "Bachelor's",
            "education_qualification": "BSc in Computer Science",
            "skills": ["Python", "SQL", "Python"],
            "salary_disclosed": disclosed,
        }

    def test_salary_disclosure_requires_numeric_source_evidence(self):
        result = validate_extraction(self._valid_extraction(disclosed=True), "Competitive compensation")
        self.assertFalse(result["salary_disclosed"])
        result = validate_extraction(self._valid_extraction(disclosed=True), "Salary: $50,000-$60,000 USD")
        self.assertTrue(result["salary_disclosed"])

    def test_malformed_extraction_is_rejected(self):
        data = self._valid_extraction()
        del data["skills"]
        with self.assertRaises(ExtractionValidationError):
            validate_extraction(data, "description")

    def test_404_recrawl_marks_active_row_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "jobs.csv")
            fields = ["job_title", "job_url", "job_status", "closed_date", "last_checked_at"]
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({"job_title": "Engineer", "job_url": "https://example.test/job/1",
                                 "job_status": "Active", "closed_date": "", "last_checked_at": ""})
            now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
            stats = recrawl_csv(path, session=FakeSession(FakeResponse(status_code=404)), now=now)
            with open(path, "r", encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(stats["closed"], 1)
            self.assertEqual(row["job_status"], "Closed")
            self.assertEqual(row["closed_date"], "2026-08-04")

    def test_active_jsonld_remains_active(self):
        html = '<script type="application/ld+json">{"@type":"JobPosting"}</script>'
        self.assertEqual(classify_response(FakeResponse(text=html), "https://example.test/job/1"), (False, "active"))

    def test_external_recognized_ats_link_is_discovered(self):
        soup = BeautifulSoup(
            '<a href="https://jobs.lever.co/example">View current openings</a>'
            '<a href="https://unrelated.test/jobs">Jobs elsewhere</a>', "html.parser")
        links = find_career_links(soup, "https://example.com", limit=10)
        self.assertEqual(links, ["https://jobs.lever.co/example"])

    def test_bare_vendor_word_does_not_make_homepage_an_ats_board(self):
        html = "<html><body><p>We leverage technology for our customers.</p></body></html>"
        self.assertEqual(_candidate_ats(object(), "https://example.com", html), (None, None))

    def test_hr_manager_listing_links_become_active_jobs(self):
        html = '''<a href="https://candidate.hr-manager.net/ApplicationInit.aspx?ProjectId=143774&amp;cid=3112">
        Ansøgningsfrist: 07. aug. 2026 Arkitekt til implementering af strategi</a>'''
        jobs = _extract_listing_jobs(html, "https://aarhus.dk/job/job-i-aarhus-kommune")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_title"], "Arkitekt til implementering af strategi")
        self.assertEqual(jobs[0]["job_status"], "Active")
        self.assertIn("ProjectId=143774", jobs[0]["job_url"])

    def test_hr_manager_table_and_detail_fields_map_to_correct_columns(self):
        listing = '''<table><tr><td>Indkøbselev</td><td>Logistics and Purchase</td>
        <td>Full-time</td><td>Danmark</td><td>August 31, 2026</td>
        <td><a href="https://candidate.hr-manager.net/ApplicationInit.aspx?ProjectId=143922">Apply</a></td>
        </tr></table>'''
        seed = _extract_listing_jobs(listing, "https://aasted.eu/career/")[0]
        detail = '''<html><head><meta property="og:title" content="Indkøbselev">
        <meta property="article:published_time" content="2026-06-11T07:40:34Z">
        <meta name="keywords" content=",Indkøbselev,Bygmarken 7-17, 3520 Farum, Denmark,Logistics and Purchase">
        </head><body><div id="AdvertisementInnerContent"><p>Full job description.</p></div>
        <div class="frist">Ansøgningsfrist 31-08-2026</div></body></html>'''
        job = _parse_hrmanager_detail(detail, seed["job_url"], seed)
        self.assertEqual(job["job_title"], "Indkøbselev")
        self.assertEqual(job["job_category"], "Logistics and Purchase")
        self.assertEqual(job["job_location"], "Danmark")
        self.assertEqual(job["employment_type"], "Full-time")
        self.assertEqual(job["application_deadline"], "2026-08-31")
        self.assertEqual(job["posted_date"], "2026-06-11")
        self.assertIn("Full job description", job["job_description"])
        self.assertEqual(job["source"], "hrmanager-detail")

    def test_hr_manager_does_not_guess_category_from_keywords(self):
        seed = empty_job()
        seed.update({"job_title": "Unsolicited application", "job_location": "Denmark",
                     "job_url": "https://candidate.hr-manager.net/ApplicationInit.aspx?ProjectId=1"})
        detail = '''<meta property="og:title" content="Unsolicited application">
        <meta name="keywords" content=",Unsolicited application,Denmark">'''
        job = _parse_hrmanager_detail(detail, seed["job_url"], seed)
        self.assertEqual(job["job_category"], "")

    def test_same_domain_career_page_requires_page_evidence(self):
        self.assertTrue(validate_career_page(
            "https://example.com/careers", "<html><h1>Join our team</h1></html>", "https://example.com"))
        self.assertFalse(validate_career_page(
            "https://example.com/about", "<html><h1>About us</h1></html>", "https://example.com"))

    def test_career_web_search_rejects_unrelated_results(self):
        results = ["https://example.com/careers", "https://jobs.lever.co/example",
                   "https://unrelated.test/jobs", "https://jobs.lever.co/another-company"]
        with patch.object(websearch, "_search_bing_rss", return_value=results):
            found = websearch.search_company_career_pages(
                "Example Ltd", "https://example.com", object(), limit=5)
        self.assertEqual(found, ["https://example.com/careers", "https://jobs.lever.co/example"])

    def test_company_without_jobs_still_gets_output_row(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "jobs.csv")
            companies = [("Example Ltd", "https://example.test", "India")]
            details = {"status": "no_jobs", "jobs": [], "source": "homepage",
                       "career_page_url": "https://example.test/careers",
                       "career_page_status": "Validated",
                       "career_page_discovery_method": "homepage_link"}
            with patch.object(pipeline, "read_companies", return_value=companies), \
                    patch.object(pipeline, "process_company_details", return_value=details):
                pipeline.run(input_file="unused.xlsx", output_file=path, workers=1, resume=False)
            with open(path, "r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["company_name"], "Example Ltd")
            self.assertEqual(rows[0]["country"], "India")
            self.assertEqual(rows[0]["website"], "https://example.test")
            self.assertEqual(rows[0]["job_status"], "No Jobs Found")
            self.assertEqual(rows[0]["career_page_url"], "https://example.test/careers")


if __name__ == "__main__":
    unittest.main()
