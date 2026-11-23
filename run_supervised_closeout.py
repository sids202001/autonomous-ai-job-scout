"""
Supervised Live Closeout Runner for Job Scout
Executes full live pipeline on real postings with pre-run diagnostics,
experience gate, relevance gate, challenge detection, and auto-submit/skip handling.
"""
import os
import sys
import time
import json
import asyncio
import logging
from datetime import datetime, timezone

# Ensure project root in sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from config import settings
from app.database import async_session
from app.models import Profile
from sqlalchemy import select
from app.services.diagnostics import PreRunDiagnostics
from app.services.audit_logger import audit_logger, SUBMISSIONS_LOG_PATH
from app.services.intelligence_loop import outcome_ledger
from app.services.auto_filler import auto_filler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("live_closeout")


async def main():
    pid = os.getpid()
    build_ts = "2026-09-17T22:10:45-04:00 (Commit bc9f7fe6a6cb9be4e85bf255c133036a88a6fb3a + Hardening Pass v2)"
    start_time_iso = datetime.now(timezone.utc).isoformat()

    print("=" * 80)
    print(f"JOB SCOUT — SUPERVISED LIVE CLOSEOUT RUN")
    print(f"Process PID: {pid}")
    print(f"Build Timestamp: {build_ts}")
    print(f"Run Started At: {start_time_iso}")
    print("=" * 80)

    # ── Step 1: Pre-Run Diagnostics Checklist ──────────────────────────────
    print("\n--- [STEP 1: PRE-RUN DIAGNOSTICS] ---")
    diag_passed, diag_report, diag_checks = await PreRunDiagnostics.run_all_checks(alert_on_failure=True)
    print(diag_report)

    if not diag_passed:
        print("\nFATAL: Pre-run diagnostics failed. Halting live closeout immediately.")
        sys.exit(1)

    # Reset run tracking and register verbatim diagnostics report for digest
    audit_logger.reset_run()
    audit_logger.set_diagnostics_report(diag_report)

    # ── Step 2: Retrieve Candidate Profile ──────────────────────────────────
    async with async_session() as session:
        profile = (await session.execute(select(Profile).limit(1))).scalar_one_or_none()
        if not profile:
            print("\nFATAL: Candidate profile not found in database. Halting.")
            sys.exit(1)
        print(f"\nLoaded candidate profile: {profile.first_name} {profile.last_name} ({profile.email})")

    # Record ledger offset before run
    ledger_path = os.path.join(settings.DATA_DIR, "outcome_ledger.json")
    ledger_entries_before = 0
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path, "r", encoding="utf-8") as f:
                ledger_entries_before = len(json.load(f))
        except Exception:
            ledger_entries_before = 0

    # ── Step 3: Define Real Live Job Postings ────────────────────────────────
    # We test 4 distinct real postings covering all gates and outcomes:
    # 1. Ramp (Ashby) -> Live Google reCAPTCHA challenge detection -> CHALLENGE_BLOCKED
    # 2. Stripe (Greenhouse) -> 5+ years seniority requirement -> SKIPPED (Experience Cap)
    # 3. Stripe (Greenhouse) -> Account Executive, AI Sales -> SKIPPED (Forbidden Domain: Sales)
    # 4. Stripe (Greenhouse) -> Backend Engineer, Payments & Risk -> ELIGIBLE -> Auto-fill & submit/verify
    postings = [
        {
            "id": 1,
            "company": "Ramp",
            "role": "Software Engineer - Core Services",
            "url": "https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245",
            "description": "Software engineer building financial infrastructure. 1-3 years of software engineering experience with Python, Go, distributed systems.",
            "mode": "auto_submit",
            "expected_gate": "Challenge Handler (Ashby live reCAPTCHA)"
        },
        {
            "id": 2,
            "company": "Stripe",
            "role": "Staff / Senior Abuse Research Engineer",
            "url": "https://stripe.com/jobs/search?gh_jid=8172503",
            "description": "Senior/Staff engineer. 5+ years of industry experience in fraud detection, risk infrastructure, distributed systems.",
            "mode": "auto_submit",
            "expected_gate": "Experience Gate (5+ years cap rejection)"
        },
        {
            "id": 3,
            "company": "Stripe",
            "role": "Account Executive, AI Sales",
            "url": "https://stripe.com/jobs/search?gh_jid=7532733",
            "description": "Drive enterprise revenue and software sales pipeline. 2-3 years of quota-carrying enterprise SaaS sales experience.",
            "mode": "auto_submit",
            "expected_gate": "Relevance Gate (Forbidden domain: sales)"
        },
        {
            "id": 4,
            "company": "Stripe",
            "role": "Backend Engineer, Payments and Risk",
            "url": "https://stripe.com/jobs/search?gh_jid=7232592",
            "description": "2+ years of industry software engineering experience. AWS, Kubernetes, Docker, GraphQL.",
            "mode": "auto_submit",
            "expected_gate": "Eligible (Full pipeline: form fill + pre-submit verification)"
        }
    ]

    print("\n--- [STEP 2: PROCESSING REAL POSTINGS THROUGH PIPELINE] ---")
    results = []

    for item in postings:
        print(f"\n{'=' * 40}")
        print(f"Touching Posting #{item['id']}: {item['company']} — {item['role']}")
        print(f"URL: {item['url']}")
        print(f"Target Gate: {item['expected_gate']}")
        print(f"{'=' * 40}")

        res = await auto_filler.open_and_prefill(
            job_url=item["url"],
            profile=profile,
            mode=item["mode"],
            job_title=item["role"],
            job_description=item["description"],
            company=item["company"]
        )
        print(f"Pipeline Result for Posting #{item['id']}: status={res.get('status')} | message={res.get('message')}")
        results.append((item, res))

    # ── Step 4: Output Verbatim End-of-Run Digest ───────────────────────────
    print("\n" + "=" * 80)
    print("VERBATIM END-OF-RUN DIGEST")
    print("=" * 80)
    digest = audit_logger.generate_run_digest()
    print(digest)
    print("=" * 80)

    # ── Step 5: Output Verbatim outcome_ledger.json Entries ──────────────────
    print("\n" + "=" * 80)
    print("VERBATIM outcome_ledger.json ENTRIES FOR THIS RUN")
    print("=" * 80)
    run_ledger_entries = []
    if os.path.exists(ledger_path):
        with open(ledger_path, "r", encoding="utf-8") as f:
            all_ledger = json.load(f)
            run_ledger_entries = all_ledger[ledger_entries_before:]
    print(json.dumps(run_ledger_entries, indent=2))
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
