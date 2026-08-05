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
from job_scraper.company import process_company_details
from job_scraper.ats import find_career_links, validate_career_page
from job_scraper import websearch
from job_scraper.parsers_generic import (_extract_listing_jobs, _parse_hrmanager_detail,
                                         _parse_generic_detail, _extract_pagination_links,
                                         _parse_inline_jobs)
from job_scraper.session import _looks_like_javascript_shell
from job_scraper.fields import empty_job
from job_scraper.fields import salary_breakdown, experience_year_range, salary_evidence_from_text
from job_scraper.parsers_jsonld import parse_microdata_jobs
from job_scraper.parsers_ats import (parse_greenhouse, parse_lever, parse_smartrecruiters,
                                     parse_recruitee, parse_breezy, parse_jazzhr,
                                     parse_workday, _personio_item, _personio_xml_item)
from job_scraper.llm_extract import process_csv
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup


class FakeResponse:
    def __init__(self, status_code=200, text="", url="https://example.test/job/1"):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = {"Content-Type": "text/html"}


class FakeSession:
    def __init__(self, response):
        self.response = response

    def fetch(self, *args, **kwargs):
        return self.response


class ApiSession:
    def __init__(self, json_values=None, post_value=None):
        self.json_values = list(json_values or [])
        self.post_value = post_value

    def fetch_json(self, *args, **kwargs):
        return self.json_values.pop(0) if self.json_values else None

    def post_json(self, *args, **kwargs):
        return self.post_value


class DataQualityTests(unittest.TestCase):
    def test_jsonld_url_falls_back_to_fetched_page(self):
        payload = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Engineer"}
        html = '<script type="application/ld+json">%s</script>' % json.dumps(payload)
        jobs = parse_jsonld_jobs(html, base_url="https://example.test/jobs/123")
        self.assertEqual(jobs[0]["job_url"], "https://example.test/jobs/123")

    def test_jsonld_and_microdata_locations_use_job_location(self):
        payload = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Engineer",
                   "jobLocation": {"address": {"addressLocality": "Pune",
                                                "addressRegion": "MH", "addressCountry": "India"}}}
        html = '<script type="application/ld+json">%s</script>' % json.dumps(payload)
        self.assertEqual(parse_jsonld_jobs(html)[0]["job_location"], "Pune, MH, India")
        remote = dict(payload, jobLocation=None, jobLocationType="TELECOMMUTE")
        html = '<script type="application/ld+json">%s</script>' % json.dumps(remote)
        self.assertEqual(parse_jsonld_jobs(html)[0]["job_location"], "Remote")
        micro = '''<div itemscope itemtype="https://schema.org/JobPosting">
        <meta itemprop="title" content="Engineer"><div itemprop="jobLocation" itemscope
        itemtype="https://schema.org/Place"><span itemprop="addressLocality">Berlin</span>
        <span itemprop="addressCountry">Germany</span></div></div>'''
        self.assertEqual(parse_microdata_jobs(micro)[0]["job_location"], "Berlin, Germany")

    def test_major_ats_parsers_preserve_locations(self):
        greenhouse = parse_greenhouse(ApiSession([{"jobs": [{"title": "A", "location": {"name": "Paris"}}]}]),
                                      {"board": "x"})[0]
        lever = parse_lever(ApiSession([[{"text": "A", "categories": {"location": "London"}}]]),
                            {"board": "x"})[0]
        smart = parse_smartrecruiters(ApiSession([{"content": [{"id": "abc", "name": "A",
            "location": {"fullLocation": "Madrid"}}], "totalFound": 1}]), {"board": "x"})[0]
        recruitee = parse_recruitee(ApiSession([{"offers": [{"title": "A", "location": "Rome"}]}]),
                                    {"board": "x"})[0]
        breezy = parse_breezy(ApiSession([[{"title": "A", "location": {"name": "Oslo"}}]]),
                              {"board": "x"})[0]
        jazz = parse_jazzhr(ApiSession([{"jobs": [{"title": "A", "city": "Boston"}]}]),
                            {"board": "x"})[0]
        workday = parse_workday(ApiSession(post_value={"jobPostings": [{"title": "A",
            "locationsText": "Toronto"}], "total": 1}),
            {"base_url": "https://x.example", "tenant": "x", "domain": "x"})[0]
        personio_json = _personio_item({"name": "A", "id": 1, "location": "Vienna"}, "x",
                                      "https://x.jobs.personio.de")
        personio_xml = _personio_xml_item(ET.fromstring(
            "<position><id>1</id><name>A</name><office>Munich</office></position>"), "x", "de")
        self.assertEqual([job["job_location"] for job in
                          (greenhouse, lever, smart, recruitee, breezy, jazz, workday,
                           personio_json, personio_xml)],
                         ["Paris", "London", "Madrid", "Rome", "Oslo", "Boston", "Toronto",
                          "Vienna", "Munich"])

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

    def test_active_page_without_jsonld_is_inconclusive_not_closed(self):
        response = FakeResponse(text="<html><h1>Engineer</h1><p>Apply now for this active role.</p></html>")
        self.assertEqual(classify_response(response, "https://example.test/job/1"),
                         (False, "active_or_inconclusive"))

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

    def test_generic_detail_maps_only_labeled_fields(self):
        html = '''<html><head><meta property="og:title" content="Data Engineer"></head><body>
        <dl><dt>Location</dt><dd>London</dd><dt>Employment Type</dt><dd>Full-time</dd>
        <dt>Application Deadline</dt><dd>August 31, 2026</dd></dl>
        <main><h2>About the role</h2><p>Apply to build reliable data platforms with our engineering team.
        This role includes pipeline ownership, testing, monitoring, documentation, and collaboration.</p></main>
        </body></html>'''
        job = _parse_generic_detail(html, "https://example.test/jobs/data-engineer")
        self.assertEqual(job["job_title"], "Data Engineer")
        self.assertEqual(job["job_location"], "London")
        self.assertEqual(job["employment_type"], "Full-time")
        self.assertEqual(job["application_deadline"], "2026-08-31")
        self.assertEqual(job["salary"], "")
        self.assertIn("pipeline ownership", job["job_description"])

    def test_inline_career_page_vacancy_is_extracted(self):
        html = '''<main><h1>Karriere bei Beispiel</h1><section>
        <h2>Objektleiter Gebäudereinigung / Gebäudemanagement (m/w/d)</h2>
        <h3>Ihre Aufgaben</h3><p>Selbstständige Leitung, Organisation und Betreuung
        infrastruktureller Objekte sowie Führung der Mitarbeiter vor Ort.</p>
        <h3>Ihr Profil</h3><p>Mehrjährige Erfahrung und Führerschein Klasse B.</p>
        <h3>Interessiert?</h3><p>Wir freuen uns auf Ihre Bewerbungsunterlagen an
        jobs@example.test.</p></section><h2>Weitere Informationen</h2></main>'''
        jobs = _parse_inline_jobs(html, "https://example.test/stellenangebote/")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_title"],
                         "Objektleiter Gebäudereinigung / Gebäudemanagement (m/w/d)")
        self.assertEqual(jobs[0]["job_status"], "Active")
        self.assertEqual(jobs[0]["source"], "inline-career-page")
        self.assertIn("Mehrjährige Erfahrung", jobs[0]["job_description"])

    def test_same_domain_career_page_requires_page_evidence(self):
        self.assertTrue(validate_career_page(
            "https://example.com/careers", "<html><h1>Join our team</h1></html>", "https://example.com"))
        self.assertFalse(validate_career_page(
            "https://example.com/about", "<html><h1>About us</h1></html>", "https://example.com"))
        soft_404 = '<html><h1>Page not found</h1><footer><a href="/careers">Careers</a></footer></html>'
        self.assertFalse(validate_career_page(
            "https://example.com/careers-at", soft_404, "https://example.com"))

    def test_zero_salary_and_explicit_experience_range(self):
        self.assertEqual(salary_breakdown("Salary: $0 confidential"), ("", "", ""))
        self.assertEqual(experience_year_range("3-5 years of experience"), ("3", "5"))
        self.assertEqual(experience_year_range("At least 4 years of experience"), ("4", ""))

    def test_german_labeled_fields_and_salary_are_separated(self):
        html = '''<main><h1>Sales Manager/in</h1>
        <p><b>REGION</b>: Wien</p><p><b>ANSTELLUNGSART</b>: Unbefristet</p>
        <p><b>BERUFSFELD</b>: Vertrieb</p>
        <p>Je nach Qualifikation bieten wir ein Jahresbruttogehalt von 60.000-112.000€.</p>
        <p>Bitte senden Sie Ihre Bewerbung und Ihren Lebenslauf an jobs@example.test.</p></main>'''
        job = _parse_generic_detail(html, "https://example.test/jobs/sales")
        self.assertEqual((job["job_location"], job["employment_type"], job["job_category"]),
                         ("Wien", "Unbefristet", "Vertrieb"))
        self.assertEqual((job["min_salary"], job["max_salary"], job["currency"]),
                         ("60000", "112000", "EUR"))
        self.assertIn("Jahresbruttogehalt", salary_evidence_from_text(
            "Je nach Qualifikation bieten wir ein Jahresbruttogehalt von 60.000€."))

    def test_hr_manager_uses_reordered_headers(self):
        html = '''<table><thead><tr><th>Location</th><th>Title</th><th>Deadline</th>
        <th>Type</th><th>Category</th><th>Action</th></tr></thead><tbody><tr>
        <td>Denmark</td><td>Buyer</td><td>August 31, 2026</td><td>Full-time</td><td>Purchase</td>
        <td><a href="https://candidate.hr-manager.net/ApplicationInit.aspx?ProjectId=7">Apply</a></td>
        </tr></tbody></table>'''
        job = _extract_listing_jobs(html, "https://example.test/career")[0]
        self.assertEqual((job["job_title"], job["job_location"], job["employment_type"],
                          job["job_category"], job["application_deadline"]),
                         ("Buyer", "Denmark", "Full-time", "Purchase", "2026-08-31"))
        self.assertEqual(job["extraction_confidence"], "High")

    def test_career_web_search_rejects_unrelated_results(self):
        results = ["https://example.com/careers", "https://jobs.lever.co/example",
                   "https://unrelated.test/jobs", "https://jobs.lever.co/another-company"]
        with patch.object(websearch, "_search_bing_rss", return_value=results), \
                patch.object(websearch, "_search_duckduckgo", return_value=[]):
            found = websearch.search_company_career_pages(
                "Example Ltd", "https://example.com", object(), limit=5)
        self.assertEqual(found, ["https://example.com/careers", "https://jobs.lever.co/example"])

    def test_company_job_search_merges_engines_and_filters_results(self):
        bing = ["https://irrelevant.test/news", "https://example.test/careers/job/engineer"]
        duck = ["https://jobs.lever.co/example/abc"]
        with patch.object(websearch, "_search_bing_rss", return_value=bing), \
                patch.object(websearch, "_search_duckduckgo", return_value=duck):
            found = websearch.search_company_job_pages(
                "Example Ltd", "https://example.test", object(), country="India")
        self.assertIn("https://example.test/careers/job/engineer", found)
        self.assertIn("https://jobs.lever.co/example/abc", found)
        self.assertNotIn("https://irrelevant.test/news", found)

    def test_unreachable_website_uses_company_name_job_search(self):
        job = empty_job()
        job.update({"job_title": "Engineer", "job_url": "https://jobs.example.test/engineer",
                    "job_status": "Active", "source": "generic-detail"})
        class SearchSession:
            def fetch(self, *args, **kwargs):
                return None
            def fetch_text(self, url, **kwargs):
                return "<main><h1>Example Engineer</h1><p>Job description and qualifications. Apply now.</p></main>"
        with patch("job_scraper.company.search_company_website", return_value=[]), \
                patch("job_scraper.company.search_company_job_pages",
                      return_value=["https://jobs.example.test/engineer"]), \
                patch("job_scraper.company.parse_generic", return_value=[job]):
            result = process_company_details(
                ("Example Ltd", "https://dead.example.test", "India"), session=SearchSession())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["career_page_discovery_method"], "internet_company_name_search")
        self.assertEqual(result["jobs"][0]["job_title"], "Engineer")

    def test_regional_no_jobs_does_not_override_official_ats(self):
        homepage = '''<a href="/en/careers/jobs/thailand/">Thailand job openings</a>
        <a href="https://example.taleo.net/careersection/2/jobsearch.ftl">All jobs</a>
        <a href="/en/careers/">Careers</a>'''
        responses = {
            "https://example.test": FakeResponse(text=homepage, url="https://example.test"),
            "https://example.test/en/careers": FakeResponse(
                text="<h1>Careers</h1><a href='https://example.taleo.net/careersection/2/jobsearch.ftl'>Jobs</a>",
                url="https://example.test/en/careers"),
            "https://example.test/en/careers/jobs/thailand": FakeResponse(
                text="<h1>Careers</h1><p>There are currently no open positions.</p>",
                url="https://example.test/en/careers/jobs/thailand"),
            "https://example.taleo.net/careersection/2/jobsearch.ftl": FakeResponse(
                text="<h1>Job Search</h1>", url="https://example.taleo.net/careersection/2/jobsearch.ftl"),
        }
        class MappingSession:
            def fetch(self, url, **kwargs):
                return responses.get(url)
            def fetch_text(self, url, **kwargs):
                response = responses.get(url)
                return response.text if response else None
        with patch("job_scraper.company.common_career_urls", return_value=[]), \
                patch("job_scraper.company.parse_generic", return_value=[]):
            result = process_company_details(("Example", "https://example.test", ""),
                                             session=MappingSession(), enable_search=False)
        self.assertEqual(result["career_page_url"], "https://example.test/en/careers")
        self.assertEqual(result["status"], "unsupported")

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

    def test_multiple_openings_create_separate_rows_with_company_data(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "jobs.csv")
            companies = [("Example Ltd", "https://example.test", "India")]
            first = empty_job()
            first.update({"job_title": "Engineer", "job_url": "https://example.test/jobs/1",
                          "job_status": "Active", "job_description": "Build systems"})
            second = empty_job()
            second.update({"job_title": "Analyst", "job_url": "https://example.test/jobs/2",
                           "job_status": "Active", "job_description": "Analyze systems"})
            details = {"status": "ok", "jobs": [first, second], "source": "generic",
                       "career_page_url": "https://example.test/careers",
                       "career_page_status": "Validated",
                       "career_page_discovery_method": "homepage_link"}
            with patch.object(pipeline, "read_companies", return_value=companies), \
                    patch.object(pipeline, "process_company_details", return_value=details):
                pipeline.run("unused.xlsx", output, workers=1, resume=False)
            with open(output, "r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual([row["job_title"] for row in rows], ["Engineer", "Analyst"])
            self.assertTrue(all(row["company_name"] == "Example Ltd" for row in rows))
            self.assertTrue(all(row["website"] == "https://example.test" for row in rows))

    def test_duplicate_domains_are_processed_once_per_run(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "jobs.csv")
            companies = [("Example One", "https://example.test", "India"),
                         ("Example Two", "https://example.test/", "India")]
            details = {"status": "no_jobs", "jobs": [], "source": "homepage",
                       "career_page_url": "https://example.test/careers",
                       "career_page_status": "Validated",
                       "career_page_discovery_method": "homepage_link"}
            with patch.object(pipeline, "read_companies", return_value=companies), \
                    patch.object(pipeline, "process_company_details", return_value=details) as mocked:
                stats = pipeline.run("unused.xlsx", output, workers=2, resume=False)
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(stats["processed"], 2)
            with open(output, "r", encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 2)

    def test_spa_shell_and_pagination_detection(self):
        shell = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
        self.assertTrue(_looks_like_javascript_shell(shell))
        html = '<main><a rel="next" href="/careers?page=2">Next</a></main>'
        self.assertEqual(_extract_pagination_links(html, "https://example.test/careers"),
                         ["https://example.test/careers?page=2"])

    def test_llm_sidecar_prevents_duplicate_calls_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "input.csv")
            output = os.path.join(directory, "output.csv")
            with open(source, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["company_name", "job_title", "job_url",
                                                            "job_description_clean"])
                writer.writeheader()
                writer.writerow({"company_name": "Example", "job_title": "Engineer",
                                 "job_url": "https://example.test/job/1",
                                 "job_description_clean": "3 years of Python experience"})
            result = {"years_of_experience_min": 3, "years_of_experience_max": "",
                      "seniority_level": "Mid", "education_stream": "", "education_type": "",
                      "education_qualification": "", "skills": "Python", "salary_disclosed": False}
            calls = []
            def fake_request(*args, **kwargs):
                calls.append(args)
                return result
            process_csv(source, output_path=output, api_key="test", request_fn=fake_request)
            process_csv(source, output_path=output, api_key="test", request_fn=fake_request)
            self.assertEqual(len(calls), 1)
            with open(output, "r", encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual((row["years_of_experience_min"], row["skills"]), ("3", "Python"))


if __name__ == "__main__":
    unittest.main()
