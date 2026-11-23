"""
Unit tests for Final Closeout:
1. Discovery Run-Lock (global mutual exclusion, skip in flight, pacing intact)
2. Digest Cleanup (no Intelligence Loop Observations, audit-access block intact)
3. Targeting (marketing & growth forbidden, 'Growth - Digital Marketing (Enterprise)' -> WRONG_FUNCTION)
4. Cap-Notice Label ('Employer Candidate Limit' for employer locks, 'Per-Company Cap Reached' for 2/180d)
5. Locked-Company Short-Circuit (skip board fetch for lockout duration, log locked until <date>)
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
from app.services.discovery_engine import DiscoveryRunLock, DiscoveryEngine, ATSEnumerationEngine
from app.services.audit_logger import AuditLogger
from app.services.relevance_gate import RelevanceGate, FORBIDDEN_DOMAIN_PATTERNS
from app.services.company_cap import CompanyCapTracker, normalize_company_name
from app.services.telegram_notifier import send_telegram_alert, reset_alert_hygiene


class Test1DiscoveryRunLock(unittest.IsolatedAsyncioTestCase):
    """Test 1: Discovery Run-Lock guarantees exactly one discovery cycle in flight."""

    async def test_run_lock_mutual_exclusion(self):
        with tempfile.NamedTemporaryFile(suffix=".lock", delete=False) as f:
            lock_path = f.name

        try:
            lock = DiscoveryRunLock(lock_path=lock_path)
            # First acquisition should succeed
            acquired1 = await lock.acquire()
            self.assertTrue(acquired1)

            # Second acquisition while held must fail
            acquired2 = await lock.acquire()
            self.assertFalse(acquired2)

            # Release lock
            lock.release()

            # Now acquisition should succeed again
            acquired3 = await lock.acquire()
            self.assertTrue(acquired3)
            lock.release()
        finally:
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                except Exception:
                    pass

    async def test_run_discovery_cycle_skips_when_locked(self):
        engine = DiscoveryEngine()
        # Hold the lock
        acquired = await engine.run_lock.acquire()
        self.assertTrue(acquired)
        try:
            res = await engine.run_discovery_cycle()
            self.assertEqual(res.get("status"), "skipped_locked")
        finally:
            engine.run_lock.release()


class Test2DigestCleanup(unittest.TestCase):
    """Test 2: Digest output contains zero Intelligence Loop Observations, retains Audit Access."""

    def setUp(self):
        self.tmp_log = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp_log.close()
        self.logger = AuditLogger(log_path=self.tmp_log.name)

    def tearDown(self):
        if os.path.exists(self.tmp_log.name):
            try:
                os.remove(self.tmp_log.name)
            except Exception:
                pass

    def test_digest_excludes_intelligence_loop_and_retains_audit_access(self):
        self.logger.log_submission(
            company="Stripe",
            role_title="Backend Engineer",
            job_url="https://jobs.lever.co/stripe/1",
            status="submitted",
            screenshot_path="/tmp/proof.png"
        )
        self.logger.log_submission(
            company="Figma",
            role_title="Growth - Digital Marketing (Enterprise)",
            job_url="https://jobs.lever.co/figma/2",
            status="skipped",
            skip_reason="WRONG_FUNCTION: disqualified_title_pattern"
        )

        digest = self.logger.generate_run_digest(window_minutes=60)

        # 1. Guaranteed removal: NO Intelligence Loop Observations
        self.assertNotIn("Intelligence Loop Observations", digest)
        self.assertNotIn("🧠 *Intelligence Loop Observations:*", digest)

        # 2. Standing rule: Audit Access block is preserved
        self.assertIn("📍 *Live Digest & Audit Access:*", digest)
        self.assertIn("• Telegram Command: `/digest`", digest)
        self.assertIn("• Submissions Log:", digest)


class Test3TargetingMarketingAndGrowth(unittest.TestCase):
    """Test 3: Marketing and growth titles resolve WRONG_FUNCTION pre-launch."""

    def test_regression_growth_digital_marketing_resolves_wrong_function(self):
        role_title = "Growth - Digital Marketing (Enterprise)"
        is_relevant, decision_reason, meta = RelevanceGate.evaluate(role_title, "")

        self.assertFalse(is_relevant)
        self.assertIn("WRONG_FUNCTION", decision_reason)
        self.assertIn("FORBIDDEN_DOMAIN", decision_reason)
        self.assertEqual(meta["relevance_score"], 0.0)

    def test_disqualified_title_regex_matches_marketing_and_growth(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("resume_bot_mod", "/Users/siddheshsawant/Desktop/resume_bot.py")
        mod = importlib.util.module_from_spec(spec)
        # Load regex from file directly to test the compiled constant
        with open("/Users/siddheshsawant/Desktop/resume_bot.py", "r") as f:
            code = f.read()

        import re
        match = re.search(r"DISQUALIFIED_TITLE_REGEX\s*=\s*(re\.compile\(.*?\n\))", code, re.DOTALL)
        self.assertIsNotNone(match)
        bot_regex = eval(match.group(1))

        # Must disqualify
        self.assertIsNotNone(bot_regex.search("Growth - Digital Marketing (Enterprise)"))
        self.assertIsNotNone(bot_regex.search("Product Marketing Manager"))
        self.assertIsNotNone(bot_regex.search("Growth Lead"))
        self.assertIsNotNone(bot_regex.search("Head of Growth"))
        self.assertIsNotNone(bot_regex.search("Digital Marketing Specialist"))
        self.assertIsNotNone(bot_regex.search("Operations Analyst"))
        self.assertIsNotNone(bot_regex.search("Executive Assistant"))

        # Must NOT disqualify verified technical roles
        self.assertIsNone(bot_regex.search("Software Engineer"))
        self.assertIsNone(bot_regex.search("Backend Engineer"))
        self.assertIsNone(bot_regex.search("Data Engineer"))
        self.assertIsNone(bot_regex.search("Machine Learning Engineer"))
        self.assertIsNone(bot_regex.search("Platform Engineer"))


class Test4CapNoticeLabel(unittest.IsolatedAsyncioTestCase):
    """Test 4: Employer candidate limits display 'Employer Candidate Limit', per-company displays 'Per-Company Cap Reached'."""

    async def test_employer_candidate_limit_alert_label(self):
        reset_alert_hygiene()
        with patch.object(settings, "TELEGRAM_SUCCESS_ONLY", False), \
             patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            # Employer candidate limit alert
            await send_telegram_alert(
                job_id="test_openai_1",
                alert_type="cap_reached",
                company="OpenAI",
                role="Software Engineer",
                portal="Ashby",
                job_url="https://jobs.ashbyhq.com/openai/1",
                details="Employer candidate limit active for OpenAI (locked for ~170 more days: OpenAI 5-applications-per-180-days candidate limit)"
            )

            # Verify the call payload
            self.assertEqual(mock_post.call_count, 1)
            call_kwargs = mock_post.call_args.kwargs
            sent_text = call_kwargs.get("json", {}).get("text", "")
            self.assertIn("*Employer Candidate Limit*", sent_text)
            self.assertNotIn("*Per-Company Cap Reached*", sent_text)

    async def test_standard_per_company_cap_alert_label(self):
        reset_alert_hygiene()
        with patch.object(settings, "TELEGRAM_SUCCESS_ONLY", False), \
             patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            # Standard 2-per-180-days cap alert
            await send_telegram_alert(
                job_id="test_stripe_1",
                alert_type="cap_reached",
                company="Stripe",
                role="Backend Engineer",
                portal="Lever",
                job_url="https://jobs.lever.co/stripe/1",
                details="Per-company cap reached: already applied 2/2 times to Stripe in the last 180 days."
            )

            self.assertEqual(mock_post.call_count, 1)
            call_kwargs = mock_post.call_args.kwargs
            sent_text = call_kwargs.get("json", {}).get("text", "")
            self.assertIn("*Per-Company Cap Reached*", sent_text)
            self.assertNotIn("*Employer Candidate Limit*", sent_text)


class Test5LockedCompanyShortCircuit(unittest.IsolatedAsyncioTestCase):
    """Test 5: Skip board fetch entirely for locked companies and log 'locked until <date>'."""

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

    def test_get_employer_lockout_active_and_expired(self):
        # 1. No cap initially
        lockout = self.tracker.get_employer_lockout("OpenAI")
        self.assertIsNone(lockout)

        # 2. Record active lock for 180 days
        self.tracker.record_employer_limit_reached("OpenAI", limit_days=180, reason="OpenAI 5-apps-per-180-days")
        lockout = self.tracker.get_employer_lockout("OpenAI")
        self.assertIsNotNone(lockout)
        capped_until, reason = lockout
        self.assertTrue(capped_until > datetime.now(timezone.utc))
        self.assertIn("OpenAI 5-apps", reason)

        # Format date
        date_str = capped_until.strftime("%Y-%m-%d")
        expected_future_year = (datetime.now(timezone.utc) + timedelta(days=170)).strftime("%Y")
        self.assertIn(expected_future_year, date_str)

    async def test_enumeration_short_circuits_locked_company(self):
        self.tracker.record_employer_limit_reached("OpenAI", limit_days=180, reason="Quota reached")
        with patch("app.services.company_cap.company_cap_tracker", self.tracker):
            enumeration = ATSEnumerationEngine()
            company_rec = {
                "name": "OpenAI",
                "canonical_domain": "openai",
                "ats_type": "ashby",
                "reachability": "reachable"
            }
            jobs = await enumeration.enumerate_company_jobs(company_rec)
            # Must return empty list immediately without making network calls
            self.assertEqual(jobs, [])


class Test6DigestDedupeAndReasonUnification(unittest.TestCase):
    """Test 6: Digest Skipped/Filtered renders each company once (deduped by normalized name, keep first entry) and unifies reason template."""

    def setUp(self):
        self.tmp_log = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
        self.tmp_log.close()
        self.logger = AuditLogger(log_path=self.tmp_log.name)

    def tearDown(self):
        if os.path.exists(self.tmp_log.name):
            try:
                os.remove(self.tmp_log.name)
            except Exception:
                pass

    def test_digest_dedupes_companies_by_normalized_name_keeping_first_entry(self):
        # 1. Multiple cycles/code paths appending OpenAI
        self.logger.log_submission(
            company="OpenAI",
            role_title="All Roles",
            job_url="N/A",
            status="skipped",
            skip_reason="EMPLOYER_LIMIT: locked until 2027-03-10"
        )
        self.logger.log_submission(
            company="OpenAI",
            role_title="Software Engineer, Core",
            job_url="https://jobs.ashbyhq.com/openai/1",
            status="skipped",
            skip_reason="Employer candidate limit active for OpenAI (locked for ~167 more days: OpenAI 5-applications-per-180-days candidate limit)"
        )
        self.logger.log_submission(
            company="OpenAI Inc.",
            role_title="Research Scientist",
            job_url="https://jobs.ashbyhq.com/openai/2",
            status="skipped",
            skip_reason="EMPLOYER_LIMIT: locked until 2027-03-10"
        )

        # 2. Deel logged across multiple cycles
        self.logger.log_submission(
            company="Deel",
            role_title="All Roles",
            job_url="N/A",
            status="skipped",
            skip_reason="UNSUPPORTED: Custom Portal / Workday without public ATS API"
        )
        self.logger.log_submission(
            company="Deel",
            role_title="All Roles",
            job_url="N/A",
            status="skipped",
            skip_reason="UNSUPPORTED: Custom Portal / Workday without public ATS API"
        )

        # 3. Stripe multiple roles skipped in same cycle
        self.logger.log_submission(
            company="Stripe",
            role_title="Staff Backend Engineer",
            job_url="https://jobs.lever.co/stripe/1",
            status="skipped",
            skip_reason="SENIORITY_EXCEEDED: Title 'Staff Backend Engineer' indicates senior/staff/principal seniority (cap: 0-3 yrs)"
        )
        self.logger.log_submission(
            company="Stripe",
            role_title="Engineering Manager",
            job_url="https://jobs.lever.co/stripe/2",
            status="skipped",
            skip_reason="SENIORITY_EXCEEDED: Title 'Engineering Manager' indicates management"
        )

        digest = self.logger.generate_run_digest()

        # Check section header
        self.assertIn("⏭ *Skipped / Filtered:*", digest)

        # OpenAI: rendered exactly once, first entry kept
        self.assertEqual(digest.count("• *OpenAI* — All Roles"), 1)
        self.assertNotIn("• *OpenAI* — Software Engineer, Core", digest)
        self.assertNotIn("• *OpenAI Inc.*", digest)

        # Deel: rendered exactly once
        self.assertEqual(digest.count("• *Deel* — All Roles"), 1)

        # Stripe: rendered exactly once, first entry kept
        self.assertEqual(digest.count("• *Stripe* — Staff Backend Engineer"), 1)
        self.assertNotIn("• *Stripe* — Engineering Manager", digest)

    def test_reason_format_string_unification(self):
        # Employer limit date extraction
        r1 = AuditLogger.unify_skip_reason("Employer candidate limit active for OpenAI (locked for ~167 more days: OpenAI 5-applications-per-180-days candidate limit)")
        self.assertTrue(r1.startswith("EMPLOYER_LIMIT: locked until "))

        # Unsupported
        r2 = AuditLogger.unify_skip_reason("UNSUPPORTED: Custom Portal / Workday without public ATS API")
        self.assertEqual(r2, "UNSUPPORTED: Custom Portal / Workday without public ATS API")

        # Company cap
        r3 = AuditLogger.unify_skip_reason("Per-company cap reached: already applied 2/2 times to Stripe in the last 180 days.")
        self.assertEqual(r3, "COMPANY_CAP: Per-company cap reached: already applied 2/2 times to Stripe in the last 180 days.")

        # Seniority
        r4 = AuditLogger.unify_skip_reason("SENIORITY_EXCEEDED: Title 'Staff' indicates senior seniority")
        self.assertEqual(r4, "SENIORITY_EXCEEDED: Title 'Staff' indicates senior seniority")

        # Wrong function
        r5 = AuditLogger.unify_skip_reason("WRONG_FUNCTION: FORBIDDEN_DOMAIN: Title falls into non-target domain")
        self.assertEqual(r5, "WRONG_FUNCTION: FORBIDDEN_DOMAIN: Title falls into non-target domain")

        # Validation failed
        r6 = AuditLogger.unify_skip_reason("Validation blocked before submit: LLM Pre-Submit Audit failed")
        self.assertEqual(r6, "VALIDATION_FAILED: LLM Pre-Submit Audit failed")

        # Fallback
        r7 = AuditLogger.unify_skip_reason("Missing required compliance field")
        self.assertEqual(r7, "FILTERED: Missing required compliance field")


if __name__ == "__main__":
    unittest.main()
