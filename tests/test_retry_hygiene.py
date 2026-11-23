"""
Regression test suite for RETRY HYGIENE & PER-COMPANY STATE ISOLATION:
1. Attempt cap: Each role gets a maximum of TWO attempts total — third attempt is impossible.
2. Retry queue: If a submission fails (NOT spam-flagged), retries exactly once after 4-hour cooldown; terminal after retry.
3. Spam-flag exclusion: Spam-flagged applications are strictly EXCLUDED from auto-retry.
4. Browser state hygiene: Clear browser cookies, cache, and site data between companies.
"""
import os
import sys
import time
import tempfile
import sqlite3
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/siddheshsawant/sid_job_scrip")
sys.path.insert(0, "/Users/siddheshsawant/Desktop")

from app.services.retry_queue import (
    RetryQueueManager,
    clear_company_browser_state,
    RETRY_COOLDOWN_SECONDS,
    MAX_ATTEMPTS_PER_ROLE
)


class TestRetryHygiene(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()
        self.mgr = RetryQueueManager(db_path=self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    # ========================================================================
    # TEST 1: Third Attempt is Impossible (Attempt Cap: Max 2 Tries Total)
    # ========================================================================
    def test_third_attempt_is_impossible(self):
        company = "Datadog"
        title = "Software Engineer"
        url = "https://jobs.lever.co/datadog/abc-123"
        base_time = 1700000000.0

        # --- Attempt 1 (Initial attempt) ---
        can_attempt, reason, attempt_num = self.mgr.can_attempt_role(company, title, url, current_time=base_time)
        self.assertTrue(can_attempt, "Initial attempt must be admitted")
        self.assertEqual(attempt_num, 1)

        # Record attempt 1 start
        attempt_rec = self.mgr.record_attempt_start(company, title, url)
        self.assertEqual(attempt_rec, 1)

        # Attempt 1 fails (technical failure)
        ok, res_msg = self.mgr.record_attempt_result(
            company=company,
            title=title,
            url=url,
            status="FAILED",
            reason="Network timeout on submit click",
            is_spam_flag=False,
            current_time=base_time
        )
        self.assertTrue(ok)
        self.assertIn("QUEUED_FOR_RETRY_AFTER_4H", res_msg)

        # Before 4h: Must be blocked by 4h cooldown
        can_retry_early, early_reason, _ = self.mgr.can_attempt_role(company, title, url, current_time=base_time + 7200)
        self.assertFalse(can_retry_early, "Must be blocked before 4-hour cooldown expires")
        self.assertIn("COOLDOWN_ACTIVE", early_reason)

        # --- Attempt 2 (Retry attempt after 4-hour cooldown) ---
        retry_time = base_time + RETRY_COOLDOWN_SECONDS + 10  # 4 hours + 10s
        can_retry, retry_reason, attempt_num2 = self.mgr.can_attempt_role(company, title, url, current_time=retry_time)
        self.assertTrue(can_retry, "Single retry must be admitted after 4-hour cooldown")
        self.assertEqual(attempt_num2, 2)

        # Record attempt 2 start
        attempt_rec2 = self.mgr.record_attempt_start(company, title, url)
        self.assertEqual(attempt_rec2, 2)

        # Attempt 2 fails
        ok2, res_msg2 = self.mgr.record_attempt_result(
            company=company,
            title=title,
            url=url,
            status="FAILED",
            reason="Submission verification failed after retry",
            is_spam_flag=False,
            current_time=retry_time
        )
        self.assertFalse(ok2)
        self.assertEqual(res_msg2, "FAILED_TERMINAL_MAX_ATTEMPTS_REACHED")

        # --- Attempt 3 Check (Strictly Prohibited: Third Attempt Impossible) ---
        post_time = retry_time + 86400  # 24 hours later
        can_attempt_3, reason_3, final_count = self.mgr.can_attempt_role(company, title, url, current_time=post_time)
        self.assertFalse(can_attempt_3, "Third attempt must be strictly blocked")
        self.assertTrue(
            "TERMINAL_BLOCKED" in reason_3 or "ATTEMPT_CAP_REACHED" in reason_3,
            f"Expected TERMINAL_BLOCKED or ATTEMPT_CAP_REACHED, got: {reason_3}"
        )
        self.assertEqual(final_count, 2, "Attempt count must remain capped at 2")

        # Verify retry queue is completely empty
        pending = self.mgr.get_pending_retries(current_time=post_time + 100000)
        self.assertEqual(len(pending), 0, "No pending retries may exist for role after 2 attempts")

        # Verify role_attempts database record is terminal
        info = self.mgr.get_role_attempt_info(company, title, url)
        self.assertIsNotNone(info)
        self.assertEqual(info["attempt_count"], 2)
        self.assertEqual(info["terminal"], 1)
        self.assertIn("FAILED_TERMINAL_AFTER_2_ATTEMPTS", info["terminal_reason"])

    # ========================================================================
    # TEST 2: Spam-Flagged Role Never Auto-Retries (Spam-Flag Exclusion)
    # ========================================================================
    def test_spam_flagged_role_never_auto_retries(self):
        company = "AshbyCorp"
        title = "Backend Engineer"
        url = "https://jobs.ashbyhq.com/ashbycorp/789"
        base_time = 1700000000.0

        # Attempt 1 starts
        can_attempt, _, _ = self.mgr.can_attempt_role(company, title, url, current_time=base_time)
        self.assertTrue(can_attempt)
        self.mgr.record_attempt_start(company, title, url)

        # Ashby detects automated application and returns "flagged as possible spam"
        ok, res_msg = self.mgr.record_attempt_result(
            company=company,
            title=title,
            url=url,
            status="FAILED",
            reason="flagged as possible spam",
            is_spam_flag=True,
            current_time=base_time
        )
        self.assertFalse(ok)
        self.assertEqual(res_msg, "SPAM_FLAG_EXCLUDED_FROM_AUTO_RETRY")

        # Assert retry queue has ZERO entries for this role
        pending_now = self.mgr.get_pending_retries(current_time=base_time)
        self.assertEqual(len(pending_now), 0, "Spam-flagged role must NEVER enter retry queue")

        # Assert retry queue is STILL empty after 4 hours, 10 hours, 30 days
        pending_4h = self.mgr.get_pending_retries(current_time=base_time + RETRY_COOLDOWN_SECONDS + 3600)
        self.assertEqual(len(pending_4h), 0, "Spam-flagged role must NEVER be queued for retry")
        pending_30d = self.mgr.get_pending_retries(current_time=base_time + 30 * 86400)
        self.assertEqual(len(pending_30d), 0)

        # Assert can_attempt_role returns False forever (manual submit IS the retry)
        can_retry_4h, reason_4h, _ = self.mgr.can_attempt_role(company, title, url, current_time=base_time + RETRY_COOLDOWN_SECONDS + 10)
        self.assertFalse(can_retry_4h, "Spam-flagged role must never auto-retry")
        self.assertIn("TERMINAL_BLOCKED", reason_4h)
        self.assertIn("SPAM_FLAG_MANUAL_SUBMIT", reason_4h)

        info = self.mgr.get_role_attempt_info(company, title, url)
        self.assertEqual(info["terminal"], 1)
        self.assertEqual(info["last_status"], "SPAM_FLAGGED")

    # ========================================================================
    # TEST 3: Browser State Cleared Between Companies (Per-Company Hygiene)
    # ========================================================================
    async def test_browser_state_cleared_between_companies(self):
        mock_context = MagicMock()
        mock_context.clear_cookies = AsyncMock()
        mock_context.clear_permissions = AsyncMock()

        mock_page = MagicMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.evaluate = AsyncMock()
        mock_page.context = mock_context

        mock_browser = MagicMock()
        mock_browser.contexts = [mock_context]

        company_name = "Cloudflare"
        res = await clear_company_browser_state(
            browser=mock_browser,
            context=mock_context,
            page=mock_page,
            company=company_name
        )
        self.assertTrue(res)

        # 1. Assert context cookies were cleared
        mock_context.clear_cookies.assert_awaited_once()

        # 2. Assert context permissions were cleared
        mock_context.clear_permissions.assert_awaited_once()

        # 3. Assert client-side storage (localStorage, sessionStorage) was cleared
        mock_page.evaluate.assert_awaited_once()
        eval_script = mock_page.evaluate.call_args[0][0]
        self.assertIn("localStorage.clear()", eval_script)
        self.assertIn("sessionStorage.clear()", eval_script)

    async def test_cleanup_browser_with_hygiene_wrapper(self):
        from resume_bot import cleanup_browser_with_hygiene

        mock_context = MagicMock()
        mock_context.clear_cookies = AsyncMock()
        mock_context.clear_permissions = AsyncMock()

        mock_page = MagicMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.evaluate = AsyncMock()
        mock_page.context = mock_context

        mock_browser = MagicMock()
        mock_browser.contexts = [mock_context]
        mock_browser.close = AsyncMock()

        mock_playwright = MagicMock()
        mock_playwright.stop = AsyncMock()

        await cleanup_browser_with_hygiene(
            p=mock_playwright,
            browser=mock_browser,
            page=mock_page,
            company="Stripe"
        )

        mock_context.clear_cookies.assert_awaited_once()
        mock_browser.close.assert_awaited_once()
        mock_playwright.stop.assert_awaited_once()

    # ========================================================================
    # TEST 4: Successful Submission Resolves Terminally Immediately
    # ========================================================================
    def test_successful_submission_resolves_terminally(self):
        company = "Ramp"
        title = "Full Stack Engineer"
        url = "https://jobs.ashbyhq.com/ramp/123"
        base_time = 1700000000.0

        can_attempt, _, _ = self.mgr.can_attempt_role(company, title, url, current_time=base_time)
        self.assertTrue(can_attempt)
        self.mgr.record_attempt_start(company, title, url)

        ok, msg = self.mgr.record_attempt_result(
            company=company,
            title=title,
            url=url,
            status="SUBMITTED",
            reason="Verified submission confirmation on page",
            is_spam_flag=False,
            current_time=base_time
        )
        self.assertTrue(ok)
        self.assertEqual(msg, "SUBMITTED_TERMINAL")

        # Never retry a submitted role
        can_retry, reason, _ = self.mgr.can_attempt_role(company, title, url, current_time=base_time + 86400)
        self.assertFalse(can_retry)
        self.assertIn("TERMINAL_BLOCKED", reason)

        # Retry queue is empty
        pending = self.mgr.get_pending_retries(current_time=base_time + 86400)
        self.assertEqual(len(pending), 0)

    # ========================================================================
    # TEST 5: Cooldown Enforcement & Query Logic for Background Worker
    # ========================================================================
    def test_cooldown_enforcement_and_retry_queue_worker_query(self):
        company = "Figma"
        title = "Systems Engineer"
        url = "https://jobs.lever.co/figma/999"
        base_time = 1700000000.0

        # Attempt 1
        self.mgr.record_attempt_start(company, title, url)
        self.mgr.record_attempt_result(
            company=company,
            title=title,
            url=url,
            status="FAILED",
            reason="Transient 502 Bad Gateway",
            is_spam_flag=False,
            current_time=base_time
        )

        # At T + 1 hour (3600s): get_pending_retries returns 0 items (cooldown not met)
        pending_1h = self.mgr.get_pending_retries(current_time=base_time + 3600)
        self.assertEqual(len(pending_1h), 0)

        # At T + 3 hours 59 min (14340s): get_pending_retries returns 0 items
        pending_almost = self.mgr.get_pending_retries(current_time=base_time + 14340)
        self.assertEqual(len(pending_almost), 0)

        # At T + 4 hours (14400s): get_pending_retries returns exactly 1 item
        pending_4h = self.mgr.get_pending_retries(current_time=base_time + 14400)
        self.assertEqual(len(pending_4h), 1)
        item = pending_4h[0]
        self.assertEqual(item["company"], company)
        self.assertEqual(item["title"], title)
        self.assertEqual(item["url"], url)

        # Mark in progress
        self.mgr.mark_retry_in_progress(item["queue_id"])
        # While in progress, get_pending_retries will NOT return it
        self.assertEqual(len(self.mgr.get_pending_retries(current_time=base_time + 14400)), 0)

        # After attempt 2 completes and fails, mark resolved / record attempt result
        self.mgr.record_attempt_start(company, title, url)
        self.mgr.record_attempt_result(
            company=company,
            title=title,
            url=url,
            status="FAILED",
            reason="Verification failed again",
            is_spam_flag=False,
            current_time=base_time + 14405
        )
        self.mgr.mark_retry_resolved(item["queue_id"])

        # Role is now terminal; can_attempt_role must return False
        can_attempt, reason, _ = self.mgr.can_attempt_role(company, title, url, current_time=base_time + 20000)
        self.assertFalse(can_attempt)
        self.assertIn("TERMINAL_BLOCKED", reason)


if __name__ == "__main__":
    unittest.main()
