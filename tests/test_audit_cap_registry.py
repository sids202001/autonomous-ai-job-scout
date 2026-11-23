"""
Unit tests for the 6 Advanced Capabilities:
1. Per-Company Application Caps & Employer Candidate Limit Tracking
2. Submission Audit Logging & End-of-Run Digest
3. Universal COMMON_FIELDS Registry & Self-Learning Field Memory
4. Telegram-Only Review Flow
5. Global Telegram Message Hygiene & State-Aware Pacing Suppression
6. Live Gmail 2FA Code Retrieval with Zero Persistent Secrets
"""
import os
import time
import asyncio
import tempfile
import sqlite3
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock, MagicMock

import config
from config import settings
from app.services.company_cap import CompanyCapTracker, normalize_company_name, extract_ats_domain
from app.services.audit_logger import AuditLogger
from app.services.common_fields import CommonFieldResolver
from app.services.telegram_notifier import (
    send_telegram_alert,
    reset_alert_hygiene,
    set_job_state,
    get_job_state
)
from app.services.code_fetcher import _poll_code_via_imap, prove_live_gmail_retrieval


class Test1PerCompanyApplicationCaps(unittest.TestCase):
    """Test 1: Per-company caps block 3rd application in window and re-allow after expiry."""

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.tracker = CompanyCapTracker(db_path=self.tmp_db.name)

    def tearDown(self):
        if os.path.exists(self.tmp_db.name):
            try:
                os.remove(self.tmp_db.name)
            except Exception:
                pass

    def test_company_normalization_and_ats_extraction(self):
        self.assertEqual(normalize_company_name("OpenAI Inc."), "openai")
        self.assertEqual(normalize_company_name("Anthropic Technologies LLC"), "anthropic")
        self.assertEqual(extract_ats_domain("https://jobs.ashbyhq.com/openai/708121e8/application"), "ashby:openai")
        self.assertEqual(extract_ats_domain("https://jobs.lever.co/benchling/123"), "lever:benchling")
        self.assertEqual(extract_ats_domain("https://boards.greenhouse.io/airbnb/jobs/456"), "greenhouse:airbnb")

    def test_per_company_cap_blocks_3rd_app_in_window_and_reallows_after_expiry(self):
        comp = "Acme Robotics"
        url = "https://jobs.ashbyhq.com/acme/app1"

        # Initially 0 applications: under cap
        capped, reason, count = self.tracker.is_company_capped(comp, url)
        self.assertFalse(capped)
        self.assertEqual(count, 0)

        # 1st application (simulated 2 hours ago to satisfy 1-hour cooldown)
        self.tracker.record_submission(comp, "Software Engineer", url, "submitted")
        with sqlite3.connect(self.tmp_db.name) as conn:
            two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            conn.execute("UPDATE company_submissions SET applied_at = ?", (two_hours_ago,))
            conn.commit()

        capped, reason, count = self.tracker.is_company_capped(comp, url)
        self.assertFalse(capped)
        self.assertEqual(count, 1)

        # 2nd application (simulated 1.5 hours ago, reaching cap of 2)
        self.tracker.record_submission(comp, "Backend Engineer", "https://jobs.ashbyhq.com/acme/app2", "submitted")
        with sqlite3.connect(self.tmp_db.name) as conn:
            one_point_five_hours_ago = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
            conn.execute("UPDATE company_submissions SET applied_at = ? WHERE role_title = 'Backend Engineer'", (one_point_five_hours_ago,))
            conn.commit()

        capped, reason, count = self.tracker.is_company_capped(comp, "https://jobs.ashbyhq.com/acme/app3")
        self.assertTrue(capped)
        self.assertEqual(count, 2)
        self.assertIn("Per-company cap reached", reason)

        # Re-allows after window expiry (mocking applied_at to 181 days ago)
        with sqlite3.connect(self.tmp_db.name) as conn:
            expired_time = (datetime.now(timezone.utc) - timedelta(days=181)).isoformat()
            conn.execute("UPDATE company_submissions SET applied_at = ?", (expired_time,))
            conn.commit()

        capped, reason, count = self.tracker.is_company_capped(comp, "https://jobs.ashbyhq.com/acme/app3")
        self.assertFalse(capped)
        self.assertEqual(count, 0)

    def test_per_company_1_hour_cooldown_blocks_immediate_repeat(self):
        comp = "Acme Robotics"
        url = "https://jobs.ashbyhq.com/acme/app1"
        self.tracker.record_submission(comp, "Software Engineer", url, "submitted")
        capped, reason, count = self.tracker.is_company_capped(comp, url)
        self.assertTrue(capped)
        self.assertIn("cooldown active", reason)


    def test_employer_limit_rejection_marks_company_capped(self):
        comp = "OpenAI"
        url = "https://jobs.ashbyhq.com/openai/app1"

        # Record employer candidate limit (e.g. OpenAI 5 applications per 180 days)
        self.tracker.record_employer_limit_reached(
            comp,
            url,
            limit_days=180,
            reason="Candidates may not apply more than 5 times in any 180 day span"
        )

        capped, reason, count = self.tracker.is_company_capped(comp, url)
        self.assertTrue(capped)
        self.assertIn("Employer candidate limit active", reason)


class Test2SubmissionReviewAndTargetingAudit(unittest.TestCase):
    """Test 2: Auditable submissions.log logging and Telegram run digest."""

    def setUp(self):
        self.tmp_log = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
        self.tmp_log.close()
        self.audit = AuditLogger(log_path=self.tmp_log.name)

    def tearDown(self):
        if os.path.exists(self.tmp_log.name):
            try:
                os.remove(self.tmp_log.name)
            except Exception:
                pass

    def test_digest_lists_every_submission_with_match_reason(self):
        self.audit.reset_run()

        # Record 1 submitted
        self.audit.log_submission(
            company="Anthropic",
            role_title="Systems Engineer",
            job_url="https://jobs.lever.co/anthropic/1",
            match_reason="Distributed Systems / Python Match",
            match_score=0.95,
            status="submitted"
        )

        # Record 1 skipped
        self.audit.log_submission(
            company="OpenAI",
            role_title="Research Engineer",
            job_url="https://jobs.ashbyhq.com/openai/1",
            match_reason="AI Match",
            match_score=0.88,
            status="skipped",
            skip_reason="Per-company cap reached (2/2 applications in 180d window)"
        )

        digest = self.audit.generate_run_digest()
        self.assertIn("*Submitted Applications:* 1", digest)
        self.assertIn("*Skipped / Blocked Roles:* 1", digest)
        self.assertIn("Anthropic", digest)
        self.assertIn("Systems Engineer", digest)
        self.assertIn("Distributed Systems / Python Match", digest)
        self.assertIn("OpenAI", digest)
        self.assertIn("Per-company cap reached", digest)

        # Verify disk log was written
        with open(self.tmp_log.name, "r") as f:
            content = f.read()
            self.assertIn("SUBMITTED", content)
            self.assertIn("SKIPPED", content)
            self.assertIn("Anthropic", content)
            self.assertIn("OpenAI", content)

    def test_require_manual_approval_config_flag(self):
        with patch.dict(os.environ, {"REQUIRE_MANUAL_APPROVAL": "true"}):
            new_settings = config.Settings()
            self.assertTrue(new_settings.REQUIRE_MANUAL_APPROVAL)


class Test3UniversalCommonFieldCoverage(unittest.TestCase):
    """Test 3: Registry resolves common fields on all ATS parsers and remembers learned answers."""

    def setUp(self):
        self.tmp_json = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp_json.close()
        self.resolver = CommonFieldResolver(learned_file=self.tmp_json.name)

    def tearDown(self):
        if os.path.exists(self.tmp_json.name):
            try:
                os.remove(self.tmp_json.name)
            except Exception:
                pass

    def test_registry_resolves_common_fields_on_all_ats_parsers(self):
        # 1. Years of experience (e.g. Replit question)
        ans1 = self.resolver.solve("How many years of relevant professional experience do you have?")
        self.assertEqual(ans1, "2")

        # 2. Years of experience with select choices
        ans2 = self.resolver.solve("Years of experience", choices=["0-1 years", "1-3 years", "3-5 years", "5+ years"])
        self.assertEqual(ans2, "1-3 years")

        # 3. Notice period
        ans3 = self.resolver.solve("What is your current notice period?")
        from form_filler import compute_start_date
        self.assertEqual(ans3, compute_start_date())

        # 4. Salary
        ans4 = self.resolver.solve("What are your salary requirements?")
        self.assertTrue("100,000" in ans4 or "100000" in ans4)

        # 5. Hybrid office attendance
        ans5 = self.resolver.solve("Are you able to work from our office 3 days a week?")
        self.assertEqual(ans5, "Yes")

        # 6. Location
        ans6 = self.resolver.solve("Candidate Location")
        self.assertIn("Washington", ans6)

        # 7. Privacy policy
        ans7 = self.resolver.solve("Candidate Privacy Notice & Consent to processing personal data", ["I acknowledge and agree", "No"])
        self.assertEqual(ans7, "I acknowledge and agree")

    def test_second_occurrence_of_halted_field_resolves_without_halting(self):
        custom_question = "What is your primary cloud orchestration tool?"

        # First time: unmapped
        first_solve = self.resolver.solve(custom_question)
        self.assertIsNone(first_solve)

        # User provides answer via Telegram manual review
        self.resolver.save_learned_field(custom_question, "AWS Step Functions & ECS")

        # Second time: resolves immediately from persistent memory without halting!
        second_solve = self.resolver.solve(custom_question)
        self.assertEqual(second_solve, "AWS Step Functions & ECS")


class Test4GlobalTelegramMessageHygiene(unittest.TestCase):
    """Test 4: Causal ordering, state-aware pacing suppression, and exactly-once dedupe."""

    def setUp(self):
        reset_alert_hygiene()
        self.httpx_patcher = patch("httpx.AsyncClient.post", new_callable=AsyncMock)
        self.mock_post = self.httpx_patcher.start()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}
        self.mock_post.return_value = mock_resp
        self.success_patcher = patch.object(settings, "TELEGRAM_SUCCESS_ONLY", False)
        self.success_patcher.start()

    def tearDown(self):
        self.httpx_patcher.stop()
        self.success_patcher.stop()

    def test_dedupe_prevents_double_alerts(self):
        job_id = "test_job_101"
        res1 = asyncio.run(send_telegram_alert(
            job_id=job_id,
            alert_type="cap_reached",
            company="Stripe",
            role="Infrastructure Engineer",
            portal="Greenhouse",
            job_url="https://boards.greenhouse.io/stripe/1"
        ))
        self.assertTrue(res1)

        # Immediate duplicate attempt for same (job_id, alert_type) is suppressed
        res2 = asyncio.run(send_telegram_alert(
            job_id=job_id,
            alert_type="cap_reached",
            company="Stripe",
            role="Infrastructure Engineer",
            portal="Greenhouse",
            job_url="https://boards.greenhouse.io/stripe/1"
        ))
        self.assertFalse(res2)

    def test_no_pacing_message_after_any_halt_or_block(self):
        job_id = "test_job_102"

        # Bot halts for manual review
        asyncio.run(send_telegram_alert(
            job_id=job_id,
            alert_type="halt_review",
            company="Scale AI",
            role="Full Stack Engineer",
            portal="Lever",
            job_url="https://jobs.lever.co/scaleai/1",
            details="Required field missing"
        ))
        self.assertEqual(get_job_state(job_id), "halted")

        # Subsequent pacing alert must be suppressed
        pacing_sent = asyncio.run(send_telegram_alert(
            job_id=job_id,
            alert_type="pacing",
            company="Scale AI",
            role="Full Stack Engineer",
            portal="Lever",
            job_url="https://jobs.lever.co/scaleai/1",
            details="Waiting 300s"
        ))
        self.assertFalse(pacing_sent)

    def test_unverified_submission_success_is_suppressed(self):
        job_id = "test_job_103"
        # Success alert without verified confirmation is strictly blocked
        sent = asyncio.run(send_telegram_alert(
            job_id=job_id,
            alert_type="submitted",
            company="Figma",
            role="Software Engineer",
            portal="Ashby",
            job_url="https://jobs.ashbyhq.com/figma/1",
            verified_success=False
        ))
        self.assertFalse(sent)


class Test5LiveGmailCodeRetrievalWithRedaction(unittest.TestCase):
    """Test 5: Live Gmail IMAP verification code retrieval and redaction hygiene."""

    def test_imap_stage_reporting_on_failure(self):
        failure_report = {}
        # Test bad connection/auth with invalid credentials
        code = _poll_code_via_imap(
            user="invalid_user_test@gmail.com",
            password="fake_password_123",
            timeout=1,
            poll_interval=1,
            failure_report=failure_report
        )
        self.assertIsNone(code)
        self.assertIn(failure_report.get("stage"), ["auth", "connection", "timeout"])

    def test_live_gmail_retrieval_extracts_test_code_with_redaction(self):
        """Live verification against real mailbox if credentials present."""
        if settings.GMAIL_USER and settings.GMAIL_APP_PASSWORD:
            res = prove_live_gmail_retrieval(settings.GMAIL_USER, settings.GMAIL_APP_PASSWORD, timeout=30)
            self.assertTrue(res.get("smtp_sent"))
            self.assertTrue(res.get("verified_match"))
            self.assertEqual(res.get("test_code_redacted"), "[REDACTED]")
            # Ensure no plain numeric code leaked in audit keys
            self.assertNotIn("test_code", res)


if __name__ == "__main__":
    unittest.main()
