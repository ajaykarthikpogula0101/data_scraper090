import argparse
import json
import os
import sys
import pprint

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from job_scraper.config import INPUT_FILE, OUTPUT_FILE
from job_scraper import pipeline


def main():
    ap = argparse.ArgumentParser(description="Company job posting scraper")
    ap.add_argument("--input", default=INPUT_FILE, help="Input Excel file")
    ap.add_argument("--output", default=OUTPUT_FILE, help="Output CSV file")
    ap.add_argument("--limit", type=int, default=0, help="Max companies to process (0=all)")
    ap.add_argument("--offset", type=int, default=0, help="Skip first N companies")
    ap.add_argument("--workers", type=int, default=10, help="Concurrent workers")
    ap.add_argument("--no-resume", action="store_true", help="Ignore existing output rows")
    ap.add_argument("--countries", default="", help="Comma-separated country filter")
    ap.add_argument("--no-search", action="store_true", help="Disable website lookup by company name")
    ap.add_argument("--retry-failures", action="store_true",
                    help="Reprocess prior Unreachable/Not Found/Unsupported company rows")
    ap.add_argument("--verify", metavar="COMPANY_NAME", help="Run a single company and print its jobs")
    args = ap.parse_args()

    countries = [c.strip() for c in args.countries.split(",") if c.strip()] if args.countries else None

    if args.verify:
        from job_scraper.company import process_company_details
        from job_scraper.pipeline import read_companies
        companies = read_companies(args.input)
        match = [c for c in companies if c[0].strip().lower() == args.verify.strip().lower()]
        if not match:
            print("Company not found in input file.")
            return 1
        name, web, country = match[0]
        print("Processing: %s | %s | %s" % (name, web, country))
        details = process_company_details((name, web, country))
        status, jobs, source = details["status"], details["jobs"], details["source"]
        print("status:", status, "| source:", source, "| jobs:", len(jobs))
        print("career page:", details["career_page_url"] or "not found",
              "| validation:", details["career_page_status"],
              "| method:", details["career_page_discovery_method"] or "n/a")
        for j in jobs[:20]:
            print(
                "- %s | posted=%s | emp=%s | %s"
                % (
                    j.get("job_title"),
                    j.get("posted_date"),
                    j.get("employment_type"),
                    j.get("job_url"),
                )
            )
        if len(jobs) > 20:
            print("... and %d more jobs" % (len(jobs) - 20))
        return 0

    counters = pipeline.run(
        input_file=args.input,
        output_file=args.output,
        limit=args.limit,
        offset=args.offset,
        workers=args.workers,
        resume=not args.no_resume,
        countries=countries,
        enable_search=not args.no_search,
        retry_failures=args.retry_failures,
    )
    pprint.pprint(counters)
    print("Output written to:", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
