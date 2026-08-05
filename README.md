# Company career discovery and job scraper

This project starts from company homepages, discovers and validates official career pages or recognized ATS boards, follows job listings and pagination, and writes one correctly aligned CSV row per job. Companies with no confirmed openings are retained as `No Jobs Found`; network and parsing failures remain distinct retryable statuses.

## Run the scraper

Use a new output filename after schema changes:

```powershell
cd D:\data_companies
python run_scraper.py --input COMPANYWEB29th_July.xlsx --output company_jobs_output_v2.csv --workers 25 --no-resume
```

To retry only previously transient failures while keeping successful rows:

```powershell
python run_scraper.py --input COMPANYWEB29th_July.xlsx --output company_jobs_output_v2.csv --workers 25 --retry-failures
```

For a large input, first verify a small sample with `--limit 20`. Browser rendering is automatic for JavaScript-only pages and blocked responses. Rendered pages are cached under `.browser_cache` for seven days. Twenty-five workers is a practical starting point; more workers can increase blocking and timeouts.

## Optional LLM enrichment

LLM extraction is deliberately a separate, explicit second pass. The main scraper makes no LLM calls and incurs no LLM cost.

```powershell
$env:OPENROUTER_API_KEY="your-key"
python -m job_scraper.llm_extract company_jobs_output_v2.csv --output company_jobs_enriched.csv
```

Successful enrichments are appended to a durable `.llm_results.jsonl` sidecar and checkpoint, so an interrupted run resumes without calling the model again for completed rows. The full CSV is written atomically once at the end.

## Recheck closed jobs

```powershell
python -m job_scraper.recrawl_closed_check company_jobs_output_v2.csv
```

A missing JSON-LD block alone is inconclusive and does not close a job. Definite HTTP removal, explicit closed-job evidence, or a redirect to a generic career landing page can close it.

## Important boundary

The scraper searches the supplied official site and recognized ATS links first. Its public web-search fallback is best-effort and is not a contractual search API. Production use requiring guaranteed search coverage needs a selected search provider and credentials; that external choice cannot be embedded safely in this repository. No universal scraper can guarantee coverage of every custom site, login wall, CAPTCHA, or anti-bot system, so review status and confidence fields rather than treating every empty result as proof that no jobs exist.
