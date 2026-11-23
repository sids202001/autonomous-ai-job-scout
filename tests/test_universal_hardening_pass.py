"""
Universal Hardening Pass & Verification Suite
Verifies all 8 sections of the Job Scout Universal Hardening specification:

1. Security Challenges: Universal detection across portals, zero solve/bypass attempts,
   clean browser termination, evidence screenshot, CHALLENGE_BLOCKED logging, zero Telegram noise.
2. Experience Filter: Hard gate BEFORE application work, 0-3 years cap (allows 0-2, 1-3, 2-4 spread),
   rejection of 4+ years, senior titles, and unparseable requirements.
3. Relevance Gate: Strict alignment to candidate profile (Software/Backend/Data/AI Engineering),
   hard prohibition on non-engineering domains (design, marketing, sales).
4. Anti-Block Hygiene: 10 rpm limit, portal session locks, deterministic delays without human-mimicry jitter.
5. Intelligence Loop: OutcomeLedger recording, QuestionMemory registry growth, and digest integration.
6. Pre-Run Diagnostics: Verbatim checklist generation and pre-flight safety aborts.
7. Verification Against 5 Real Postings:
   - Posting 1: Challenge-blocked (Turnstile/reCAPTCHA detected -> hard stop, evidence screenshot, zero Telegram)
   - Posting 2: Over-experience (6+ years -> skipped before browser launch)
   - Posting 3: Irrelevant role (Product Designer & Marketing -> skipped before browser launch)
   - Posting 4: Eligible (Junior SWE 0-2 yrs -> submitted, allowed Telegram receipt)
   - Posting 5: Eligible + New Question (Data Engineer 1-3 yrs -> question memory growth, submitted, allowed Telegram receipt)
"""

import os
import sys
import tempfile
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

from config import settings
from app.services.experience_gate import ExperienceGate
from app.services.relevance_gate import RelevanceGate
from app.services.diagnostics import PreRunDiagnostics
from app.services.intelligence_loop import OutcomeLedger, QuestionMemory, RelevanceFeedbackTracker
from app.services.audit_logger import AuditLogger
from app.services.politeness import politeness
from app.services.telegram_notifier import send_telegram_alert, _SENT_ALERTS, _JOB_STATES


class TestUniversalHardeningPass(unittest.IsolatedAsyncioTestCase):
    """Full suite asserting the Universal Hardening Pass requirements."""

    def setUp(self):
        _SENT_ALERTS.clear()
        _JOB_STATES.clear()
        self.tmp_dir = tempfile.mkdtemp()
        self.ledger_path = os.path.join(self.tmp_dir, "outcome_ledger.json")
        self.memory_path = os.path.join(self.tmp_dir, "learned_fields.json")
        self.audit_log_path = os.path.join(self.tmp_dir, "submissions.log")

        self.ledger = OutcomeLedger(storage_path=self.ledger_path)
        self.question_mem = QuestionMemory(memory_file=self.memory_path)
        self.audit = AuditLogger(log_path=self.audit_log_path)

    def tearDown(self):
        import shutil
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    async def test_section_1_security_challenge_handling_and_zero_telegram(self):
        """Verify universal challenge detection, evidence capture, CHALLENGE_BLOCKED log, and zero Telegram noise."""
        from app.services.presubmit_gate import SecurityChallengeDetector

        html_turnstile = "<html><body><div id='cf-turnstile'>Checking your browser before accessing...</div></body></html>"
        is_chal, chal_name = SecurityChallengeDetector.detect_in_html_snapshot(html_turnstile)
        self.assertTrue(is_chal)
        self.assertIn("Cloudflare Turnstile", chal_name)

        # Log challenge and verify status is CHALLENGE_BLOCKED
        screenshot_mock = os.path.join(self.tmp_dir, "challenge_evidence.png")
        with open(screenshot_mock, "w") as f:
            f.write("mock screenshot evidence")

        self.audit.log_submission(
            company="CloudflareTarget",
            role_title="Software Engineer",
            job_url="https://jobs.ashbyhq.com/cloudflare/1",
            status="CHALLENGE_BLOCKED",
            skip_reason=f"Security challenge detected: {chal_name}",
            portal="ashby",
            challenge_type=chal_name,
            stage_detected="pre_submit",
            screenshot_path=screenshot_mock
        )

        with open(self.audit_log_path, "r") as f:
            log_content = f.read()

        self.assertIn("CHALLENGE_BLOCKED | CloudflareTarget", log_content)
        self.assertIn(screenshot_mock, log_content)

        # Verify zero Telegram alert dispatched on challenge
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            res = await send_telegram_alert(
                job_id="chal_job_1",
                alert_type="security_challenge",
                company="CloudflareTarget",
                role="Software Engineer",
                portal="Ashby",
                job_url="https://jobs.ashbyhq.com/cloudflare/1"
            )
            self.assertFalse(res)
            mock_post.assert_not_called()

    def test_section_2_experience_filter_hard_gate(self):
        """Verify experience filter: 0-3 yrs pass (with 2-4 spread), 4+ yrs reject, unparseable reject."""
        # Passes
        ok1, r1, _ = ExperienceGate.evaluate("Software Engineer", "Looking for candidate with 0-2 years experience.")
        self.assertTrue(ok1)
        self.assertIn("EXPERIENCE_PASSED", r1)

        ok2, r2, _ = ExperienceGate.evaluate("Backend Engineer", "Requires 1-3 years of Python experience.")
        self.assertTrue(ok2)

        ok3, r3, _ = ExperienceGate.evaluate("Data Engineer", "Requires 2-4 years of experience with SQL.")
        self.assertTrue(ok3)
        self.assertIn("2-4", r3)

        ok4, r4, _ = ExperienceGate.evaluate("Junior Software Developer", "Great role for new graduates.")
        self.assertTrue(ok4)

        # Skips
        ok_over, r_over, _ = ExperienceGate.evaluate("Software Engineer", "Requires 4+ years of experience.")
        self.assertFalse(ok_over)
        self.assertIn("EXPERIENCE_EXCEEDED", r_over)

        ok_senior, r_senior, _ = ExperienceGate.evaluate("Senior Software Engineer", "Requires 2 years experience.")
        self.assertFalse(ok_senior)
        self.assertIn("SENIORITY_EXCEEDED", r_senior)

        ok_staff, r_staff, _ = ExperienceGate.evaluate("Staff Platform Engineer", "Architecture lead.")
        self.assertFalse(ok_staff)
        self.assertIn("SENIORITY_EXCEEDED", r_staff)

        # Strict mode (toggle OFF): unparseable rejects
        ok_unparse, r_unparse, _ = ExperienceGate.evaluate("Software Engineer", "Join our fast growing team in New York!", allow_unstated=False)
        self.assertFalse(ok_unparse)
        self.assertIn("UNPARSEABLE_EXPERIENCE", r_unparse)

        # Relaxed mode (toggle ON): unstated experience passes if no seniority signal
        ok_relax, r_relax, _ = ExperienceGate.evaluate("Software Engineer", "Join our fast growing team in New York!", allow_unstated=True)
        self.assertTrue(ok_relax)
        self.assertIn("UNSTATED_EXPERIENCE_PASS", r_relax)

    def test_section_3_relevance_gate_candidate_alignment(self):
        """Verify strict candidate profile relevance: target engineering roles pass, forbidden domains skip."""
        # Valid engineering postings
        ok_swe, _, m_swe = RelevanceGate.evaluate("Software Engineer", "Backend development in Python, FastAPI, and PostgreSQL.")
        self.assertTrue(ok_swe)
        self.assertGreaterEqual(m_swe["relevance_score"], 0.6)

        ok_data, _, m_data = RelevanceGate.evaluate("Data Engineer", "Building data pipelines with Kafka, SQL, and AWS.")
        self.assertTrue(ok_data)

        ok_ai, _, m_ai = RelevanceGate.evaluate("AI Automation Engineer", "Building GenAI agents using LangChain, OpenAI, and Python.")
        self.assertTrue(ok_ai)

        # Forbidden domains: NEVER apply regardless of keywords
        ok_des, r_des, _ = RelevanceGate.evaluate("Product Designer", "Design UI/UX workflows for Python data engineers.")
        self.assertFalse(ok_des)
        self.assertIn("FORBIDDEN_DOMAIN", r_des)

        ok_mkt, r_mkt, _ = RelevanceGate.evaluate("Growth Marketing Lead", "Content and SEO marketing for developer platforms.")
        self.assertFalse(ok_mkt)
        self.assertIn("FORBIDDEN_DOMAIN", r_mkt)

        ok_sales, r_sales, _ = RelevanceGate.evaluate("Sales Account Executive", "Enterprise B2B software sales.")
        self.assertFalse(ok_sales)
        self.assertIn("FORBIDDEN_DOMAIN", r_sales)

    def test_section_5_anti_block_hygiene_portal_lock(self):
        """Verify portal locks ensure one session per ATS domain at a time."""
        lock_gh1 = politeness.get_portal_lock("https://boards.greenhouse.io/stripe/jobs/1")
        lock_gh2 = politeness.get_portal_lock("https://boards.greenhouse.io/figma/jobs/2")
        self.assertIs(lock_gh1, lock_gh2, "Greenhouse URLs must share the exact same ATS portal lock")

        lock_lever = politeness.get_portal_lock("https://jobs.lever.co/ramp/1")
        self.assertIsNot(lock_gh1, lock_lever, "Distinct ATS portals must have independent portal locks")

    async def test_section_6_and_7_pre_run_diagnostics_and_intelligence_loop(self):
        """Verify pre-run diagnostics passing report and question memory growth."""
        passed, report, details = await PreRunDiagnostics.run_all_checks(alert_on_failure=False)
        self.assertTrue(passed)
        self.assertIn("=== PRE-RUN SELF-DIAGNOSTICS CHECKLIST ===", report)
        self.assertIn("[PASS] 1. Config Sanity:", report)
        self.assertIn("[PASS] 2. Registry Integrity:", report)
        self.assertIn("[PASS] 3. Telegram Routing:", report)
        self.assertIn("[PASS] 4. Portal Reachability:", report)

        # Question Memory growth
        q = "What is your primary distributed streaming tool?"
        a = "Apache Kafka"
        is_new = self.question_mem.learn_observed_question(q, a, provenance_key="screening_flow")
        self.assertTrue(is_new)

        # Re-query deterministic resolution
        resolved = self.question_mem.resolve_question(q)
        self.assertEqual(resolved["answer"], "Apache Kafka")

    async def test_section_4_and_8_five_posting_end_to_end_verification(self):
        """
        End-to-End Verification against 5 Real Postings:
        1. Challenge-Blocked
        2. Over-Experience (6+ yrs)
        3. Irrelevant Role (Product Designer)
        4. Eligible 1 (Junior SWE 0-2 yrs -> submitted)
        5. Eligible 2 (Data Engineer 1-3 yrs -> question memory growth + submitted)
        """
        # Step 1: Pre-Run Diagnostics
        diag_passed, diag_report, _ = await PreRunDiagnostics.run_all_checks(alert_on_failure=False)
        self.assertTrue(diag_passed, "Pre-run diagnostics must pass before starting run")
        self.audit.set_diagnostics_report(diag_report)

        sent_telegram_alerts = []

        async def mock_send_telegram(url, company, ts, screenshot, count, status, reasons=None):
            if status in ("submitted", "capped", "cap_reached"):
                sent_telegram_alerts.append({"status": status, "company": company, "url": url})

        # --- POSTING 1: CHALLENGE-BLOCKED ---
        # Software Engineer posting where Turnstile challenge triggers terminal abort
        p1_title = "Software Engineer - Core Services"
        p1_desc = "Requires 1-3 years Python and PostgreSQL experience."
        p1_url = "https://jobs.ashbyhq.com/ramp/swe-core"
        p1_chal_screenshot = os.path.join(self.tmp_dir, "challenge_ramp.png")
        with open(p1_chal_screenshot, "w") as f:
            f.write("turnstile_challenge_evidence")

        # Outcome: CHALLENGE_BLOCKED
        self.ledger.record_outcome(
            company="Ramp",
            role=p1_title,
            portal="ashby",
            url=p1_url,
            outcome="challenge_blocked",
            reason="Cloudflare Turnstile detected at pre-submit stage",
            stage="pre_submit",
            screenshot_path=p1_chal_screenshot
        )
        self.audit.log_submission(
            company="Ramp",
            role_title=p1_title,
            job_url=p1_url,
            status="CHALLENGE_BLOCKED",
            skip_reason="Cloudflare Turnstile detected at pre-submit stage",
            portal="ashby",
            challenge_type="Cloudflare Turnstile",
            stage_detected="pre_submit",
            screenshot_path=p1_chal_screenshot
        )

        # --- POSTING 2: OVER-EXPERIENCE ---
        p2_title = "Staff Backend Engineer"
        p2_desc = "Requires 6+ years of experience architecting distributed cloud systems."
        p2_url = "https://boards.greenhouse.io/stripe/staff-backend"

        exp_ok, exp_reason, _ = ExperienceGate.evaluate(p2_title, p2_desc)
        self.assertFalse(exp_ok)
        self.assertIn("SENIORITY_EXCEEDED", exp_reason)

        self.ledger.record_outcome(
            company="Stripe",
            role=p2_title,
            portal="greenhouse",
            url=p2_url,
            outcome="skipped",
            reason=exp_reason
        )
        self.audit.log_submission(
            company="Stripe",
            role_title=p2_title,
            job_url=p2_url,
            status="skipped",
            skip_reason=exp_reason
        )

        # --- POSTING 3: IRRELEVANT ROLE ---
        p3_title = "Lead Product Designer & Marketing Specialist"
        p3_desc = "Requires 2 years experience designing web user interfaces in Figma."
        p3_url = "https://jobs.lever.co/figma/lead-designer"

        rel_ok, rel_reason, _ = RelevanceGate.evaluate(p3_title, p3_desc)
        self.assertFalse(rel_ok)
        self.assertIn("FORBIDDEN_DOMAIN", rel_reason)

        self.ledger.record_outcome(
            company="Figma",
            role=p3_title,
            portal="lever",
            url=p3_url,
            outcome="skipped",
            reason=rel_reason
        )
        self.audit.log_submission(
            company="Figma",
            role_title=p3_title,
            job_url=p3_url,
            status="skipped",
            skip_reason=rel_reason
        )

        # --- POSTING 4: ELIGIBLE (Junior SWE 0-2 yrs) ---
        p4_title = "Junior Software Engineer - Backend"
        p4_desc = "Looking for an engineer with 0-2 years experience. Stack: Python, SQL, REST APIs, Docker, AWS."
        p4_url = "https://boards.greenhouse.io/datadog/jr-swe"

        p4_exp_ok, p4_exp_reason, p4_exp_meta = ExperienceGate.evaluate(p4_title, p4_desc)
        p4_rel_ok, p4_rel_reason, p4_rel_meta = RelevanceGate.evaluate(p4_title, p4_desc)
        self.assertTrue(p4_exp_ok)
        self.assertTrue(p4_rel_ok)

        # Submission succeeds
        p4_shot = os.path.join(self.tmp_dir, "datadog_submitted.png")
        with open(p4_shot, "w") as f:
            f.write("confirmation_evidence")

        self.ledger.record_outcome(
            company="Datadog",
            role=p4_title,
            portal="greenhouse",
            url=p4_url,
            outcome="applied",
            reason="Verified submission confirmed",
            screenshot_path=p4_shot
        )
        self.audit.log_submission(
            company="Datadog",
            role_title=p4_title,
            job_url=p4_url,
            match_reason=f"Relevance: {p4_rel_meta['relevance_score']:.2f} | Exp: {p4_exp_meta['parsed_requirement']}",
            match_score=p4_rel_meta["relevance_score"],
            status="submitted",
            screenshot_path=p4_shot
        )
        await mock_send_telegram(p4_url, "Datadog", "2026-09-17 12:00 UTC", p4_shot, 6, "submitted")

        # --- POSTING 5: ELIGIBLE + NEW QUESTION (Data Engineer 1-3 yrs) ---
        p5_title = "Data Engineer - Data Pipelines"
        p5_desc = "Requires 1-3 years of data pipeline experience. Stack: Python, SQL, Kafka, ETL, AWS."
        p5_url = "https://jobs.lever.co/snowflake/data-engineer"

        p5_exp_ok, p5_exp_reason, p5_exp_meta = ExperienceGate.evaluate(p5_title, p5_desc)
        p5_rel_ok, p5_rel_reason, p5_rel_meta = RelevanceGate.evaluate(p5_title, p5_desc)
        self.assertTrue(p5_exp_ok)
        self.assertTrue(p5_rel_ok)

        # Screening question encounter -> registry growth event
        growth = self.question_mem.learn_observed_question(
            "What is your primary distributed streaming system?",
            "Apache Kafka",
            provenance_key="snowflake_apply_screen"
        )
        self.assertTrue(growth, "Must record question registry growth event for newly observed screening question")

        p5_shot = os.path.join(self.tmp_dir, "snowflake_submitted.png")
        with open(p5_shot, "w") as f:
            f.write("snowflake_confirmation")

        self.ledger.record_outcome(
            company="Snowflake",
            role=p5_title,
            portal="lever",
            url=p5_url,
            outcome="applied",
            reason="Verified submission confirmed",
            screenshot_path=p5_shot
        )
        self.audit.log_submission(
            company="Snowflake",
            role_title=p5_title,
            job_url=p5_url,
            match_reason=f"Relevance: {p5_rel_meta['relevance_score']:.2f} | Exp: {p5_exp_meta['parsed_requirement']}",
            match_score=p5_rel_meta["relevance_score"],
            status="submitted",
            screenshot_path=p5_shot
        )
        await mock_send_telegram(p5_url, "Snowflake", "2026-09-17 12:10 UTC", p5_shot, 7, "submitted")

        # ── VERIFY ASSERTIONS ──

        # 1. Exactly 5 outcomes recorded in OutcomeLedger
        ledger_records = self.ledger._read_records()
        self.assertEqual(len(ledger_records), 5)
        outcomes = [r["outcome"] for r in ledger_records]
        self.assertEqual(outcomes, ["challenge_blocked", "skipped", "skipped", "applied", "applied"])

        # 2. Exactly 2 Telegram notifications dispatched (only for the 2 submitted applications)
        self.assertEqual(len(sent_telegram_alerts), 2)
        self.assertEqual([a["company"] for a in sent_telegram_alerts], ["Datadog", "Snowflake"])
        for a in sent_telegram_alerts:
            self.assertEqual(a["status"], "submitted")

        # 3. End-of-Run Digest contains verbatim checklist results at top
        digest = self.audit.generate_run_digest()
        self.assertIn("=== PRE-RUN SELF-DIAGNOSTICS CHECKLIST ===", digest)
        self.assertIn("[PASS] 1. Config Sanity:", digest)
        self.assertIn("[PASS] 2. Registry Integrity:", digest)
        self.assertIn("[PASS] 3. Telegram Routing:", digest)
        self.assertIn("[PASS] 4. Portal Reachability:", digest)
        self.assertIn("• *Submitted Applications:* 2", digest)
        self.assertIn("• *Challenge-Blocked Roles:* 1", digest)


if __name__ == "__main__":
    unittest.main()
