"""
Regression tests for Nov 25 fixes:
1. Canonical resume path & updated CANDIDATE_FACTS (Talviro, AWS monitoring, MCP assistant, Job Scout, C++/TypeScript).
2. All 5 halted screening questions & general rules (Chime, Akunacapitaloci, Graphcore, Datalabusa, Reddit).
3. Telegram safe-halt alert formatting (no fragile legacy Markdown that triggers 400 Bad Request on job_app URLs).
4. LaunchTracker duplicate launch_id resolution (resolves newest LAUNCHED entry instead of flipping to STALLED).
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app.services.form_guards import (
    format_missing_info_request,
    is_how_did_you_hear_question,
    solve_canonical_screening_question,
    solve_how_did_you_hear,
)
from app.services.grounded_essay import CANDIDATE_FACTS
from app.services.launch_tracker import LaunchTracker


class TestNov25Fixes(unittest.TestCase):
    # --- 1. Canonical Resume & Fact Bank ---
    def test_candidate_facts_reflect_canonical_resume(self):
        self.assertIn("Talviro INC", CANDIDATE_FACTS["EXP_TALVIRO"]["text"])
        self.assertIn("Miami, FL", CANDIDATE_FACTS["EXP_TALVIRO"]["text"])
        self.assertIn("Jun 2026 - Present", CANDIDATE_FACTS["EXP_TALVIRO"]["text"])

        self.assertIn("Mar 2025 - Mar 2026", CANDIDATE_FACTS["EXP_AWS_VALIDATION"]["text"])
        self.assertIn(
            "monitoring and alerting with CloudWatch and Lambda that cut failure detection from hours to minutes",
            CANDIDATE_FACTS["EXP_AWS_MONITORING"]["text"],
        )
        self.assertIn(
            "internal MCP-based AI ticketing assistant that handles issue triage during on-call rotations",
            CANDIDATE_FACTS["EXP_AWS_MCP_ASSISTANT"]["text"],
        )
        job_scout = CANDIDATE_FACTS["PROJ_JOB_SCOUT"]["text"]
        for tok in ("Python", "Playwright", "Gemini", "PyPDF2", "FastAPI", "SQLite", "Telegram"):
            self.assertIn(tok, job_scout)

        skills = CANDIDATE_FACTS["SKILLS_STACK"]["text"]
        self.assertIn("C++", skills)
        self.assertIn("TypeScript", skills)

    # --- 2. The 5 Halted Questions & General Rules ---
    def test_chime_cisgender_question(self):
        choices = ["Cisgender", "Transgender", "I prefer to self-describe", "I don't wish to answer"]
        ans = solve_canonical_screening_question("I identify as:", choices)
        self.assertEqual(ans, "Cisgender")

    def test_akunacapital_options_101_not_yet_maps_to_no(self):
        choices = [
            "Not yet!",
            "I'm >50% through the online Options 101 course",
            "I have concluded the online Options 101 course",
        ]
        ans = solve_canonical_screening_question(
            "Did you complete our online Options 101 Course? If not, head to akunacapital.teachable.com",
            choices,
        )
        self.assertEqual(ans, "Not yet!")

    def test_graphcore_full_legal_name_confirmation(self):
        ans = solve_canonical_screening_question(
            "Have you added your full legal name and surname (including any middle names)?",
            ["Yes", "No"],
        )
        self.assertEqual(ans, "Yes")

    def test_graphcore_legal_rights_to_work_in_usa(self):
        ans = solve_canonical_screening_question(
            "Do you have legal rights to work in the USA?",
            ["Yes", "No"],
        )
        self.assertEqual(ans, "Yes")

    def test_datalabusa_job_site_source_picks_other_never_linkedin_or_referral(self):
        q = "From which job site did you see this posting?"
        choices = ["Indeed", "LinkedIN", "University", "Glassdoor", "Zip Recruiter", "Referral", "Search Engine", "Other"]
        self.assertTrue(is_how_did_you_hear_question(q))
        ans = solve_how_did_you_hear(q, choices, is_required=True)
        self.assertEqual(ans, "Other")

    def test_reddit_plural_ethnicities_picks_south_asian(self):
        q = "Please select up to 2 ethnicities that you most closely identify with."
        choices = [
            "American Indian or Alaska Native",
            "Black or African American",
            "East Asian",
            "South Asian",
            "Hispanic or Latinx",
            "Middle Eastern or North African",
            "Native Hawaiian or Other Pacific Islander",
            "White",
            "I don't wish to answer",
        ]
        ans = solve_canonical_screening_question(q, choices)
        self.assertEqual(ans, "South Asian")

    # --- 3. Telegram 400 Fix (Plain Text Safe-Halt Format) ---
    def test_format_missing_info_request_no_fragile_markdown_on_greenhouse_url(self):
        url = "https://job-boards.greenhouse.io/embed/job_app?for=chime&token=8569366002"
        msg = format_missing_info_request(
            "UNMAPPED_SCREENING_QUESTION",
            "I identify as:",
            url,
            "Chime",
            ["Cisgender", "Transgender", "I don't wish to answer"],
        )
        self.assertIn(url, msg)
        self.assertIn("Chime", msg)
        self.assertIn("Options Offered:", msg)
        # No legacy Markdown wrappers that break on unescaped underscores
        self.assertNotIn("*Company:*", msg)
        self.assertNotIn("`I identify as:`", msg)

    # --- 4. LaunchTracker Duplicate launch_id Bug Fix ---
    def test_launch_tracker_resolves_newest_entry_not_stale_historical_one(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker_file = os.path.join(tmpdir, "launch_tracker.json")
            proof_file = os.path.join(tmpdir, "build_proof.json")
            tracker = LaunchTracker(tracker_path=tracker_file, proof_path=proof_file)

            # Historical launch with same Greenhouse token (already resolved earlier)
            tracker.register_launch("Chime", "AI/ML Engineer", "https://example.com/8569366002", portal="greenhouse", launch_id="8569366002")
            tracker.resolve_launch("8569366002", status="FAILED", reason="Old attempt", sync_audit_log=False)

            # New launch with the SAME Greenhouse token
            tracker.register_launch("Chime", "AI/ML Engineer", "https://example.com/8569366002", portal="greenhouse", launch_id="8569366002")
            tracker.resolve_launch("8569366002", status="FAILED", reason="New attempt halt", sync_audit_log=False)

            # Backdate timestamps by 60m and run stalled check — newest entry MUST stay FAILED, NOT STALLED
            with open(tracker_file) as f:
                recs = json.load(f)
            old_ts = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
            for r in recs:
                r["timestamp"] = old_ts
            with open(tracker_file, "w") as f:
                json.dump(recs, f)

            stalled = tracker.check_stalled_launches(timeout_minutes=30)
            self.assertEqual(len(stalled), 0)

            with open(tracker_file) as f:
                final_recs = json.load(f)
            self.assertEqual(len(final_recs), 2)
            self.assertEqual(final_recs[0]["reason"], "Old attempt")
            self.assertEqual(final_recs[1]["status"], "FAILED")
            self.assertEqual(final_recs[1]["terminal_outcome"], "FAILED")
            self.assertEqual(final_recs[1]["reason"], "New attempt halt")


if __name__ == "__main__":
    unittest.main()
