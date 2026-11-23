#!/usr/bin/env python3
"""
Live Funnel Fix Verification Runner
Runs live scan through the actual production JobScraper, tests the full
funnel pipeline with all fixes live, and generates the verbatim digest.
"""
import sys
import os
import asyncio
from datetime import datetime, timezone

# Ensure project root in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.job_scraper import JobScraper, get_current_funnel, reset_funnel, REAL_COMPANY_BOARDS
from app.services.audit_logger import audit_logger
from app.services.diagnostics import PreRunDiagnostics
from config import settings


async def main():
    print("=" * 80)
    print("JOB SCOUT — LIVE FUNNEL VERIFICATION RUN")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print(f"ALLOW_UNSTATED_EXPERIENCE: {settings.ALLOW_UNSTATED_EXPERIENCE}")
    print(f"TITLE_MATCH_GUARANTEES_RELEVANCE: {settings.TITLE_MATCH_GUARANTEES_RELEVANCE}")
    print(f"SPONSORSHIP_UNKNOWN_PROCEEDS: {settings.SPONSORSHIP_UNKNOWN_PROCEEDS}")
    print("=" * 80)

    # 1. Run pre-run diagnostics
    print("\n--- Running Pre-Run Diagnostics ---")
    diag = PreRunDiagnostics()
    diag_ok, diag_report, _ = await diag.run_all_checks(alert_on_failure=False)
    print(diag_report)
    audit_logger.set_diagnostics_report(diag_report)

    # 2. Run scan on a representative subset of 25 companies
    # (Greenhouse, Lever, Ashby - including newly fixed handles)
    print("\n--- Running Scraper Across Representative Set (25 Companies) ---")
    scraper = JobScraper()
    # We pass empty filters so it searches all general software/data/AI engineering roles
    jobs = await scraper.search_jobs(filters=[], max_companies=25)

    funnel = get_current_funnel()

    print("\n" + "=" * 80)
    print("COMPLETE VERBATIM FUNNEL TABLE FROM LIVE SCAN")
    print("=" * 80)
    print(funnel.to_table())
    print("=" * 80)

    # 3. Simulate digest generation with audit_logger
    print("\n--- Generating End-of-Run Digest ---")
    digest = audit_logger.generate_run_digest()
    print("\n" + "=" * 80)
    print("VERBATIM END-OF-RUN DIGEST")
    print("=" * 80)
    print(digest)
    print("=" * 80)

    print(f"\nVerification Scan Finished: {len(jobs)} eligible jobs found out of {funnel.raw_postings} raw postings!")


if __name__ == "__main__":
    asyncio.run(main())
