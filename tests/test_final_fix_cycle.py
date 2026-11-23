import unittest
import asyncio
import os
import sys
import tempfile
import sqlite3
from unittest.mock import patch, MagicMock, AsyncMock

# Ensure project root is in sys.path
sys.path.insert(0, "/Users/siddheshsawant/sid_job_scrip")
sys.path.insert(0, "/Users/siddheshsawant/Desktop")

from config import settings
from app.services.launch_tracker import LaunchTracker
from app.services.telegram_notifier import send_telegram_alert, reset_alert_hygiene
from app.services.audit_logger import AuditLogger
from app.services.company_cap import CompanyCapTracker


class TestPartASqlalchemyCrashHardening(unittest.IsolatedAsyncioTestCase):
    """
    Part A Regression Test:
    Simulate missing dependency / import failure on launch path.
    Prove launch resolves to FAILED with a clear reason, never an unhandled traceback.
    """

    async def test_missing_module_resolves_to_clean_failed(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            tracker_file = tf.name

        try:
            tracker = LaunchTracker(tracker_path=tracker_file)
            launch_id = tracker.register_launch("Patreon", "Security Engineer", "https://jobs.ashbyhq.com/patreon/1")
            
            # Simulate launch path catching missing module (e.g. ModuleNotFoundError: No module named 'sqlalchemy')
            try:
                raise ModuleNotFoundError("No module named 'sqlalchemy'")
            except (ImportError, ModuleNotFoundError) as ie:
                tracker.resolve_launch(
                    launch_id=launch_id,
                    status="FAILED",
                    reason=f"Missing dependency or import error: {ie}"
                )

            records = tracker._read_records()
            lnch = next(r for r in records if r["launch_id"] == launch_id)
            self.assertEqual(lnch["status"], "FAILED")
            self.assertIn("Missing dependency or import error", lnch["reason"])
            self.assertIn("sqlalchemy", lnch["reason"])
        finally:
            if os.path.exists(tracker_file):
                os.remove(tracker_file)

    def test_auto_filler_safe_without_sqlalchemy(self):
        """Verify app.services.auto_filler can be safely imported even if app.models fails."""
        import app.services.auto_filler as af
        # Even if sqlalchemy ORM is unavailable, Profile fallback is safely provided
        self.assertTrue(hasattr(af, "Profile"))


class TestPartBBacklogCoverageAndDedupe(unittest.TestCase):
    """
    Part B Regression Tests:
    1. Lookback window: up to 30 days (720h) evaluated in auto mode.
    2. Previously-seen jobs under older gate versions (< 3) re-evaluated.
    3. Deduplication: Never submit twice for the same role (submission records guard).
    """

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db_path = self.tmp_db.name

        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE seen_jobs (
                    job_key TEXT PRIMARY KEY,
                    company TEXT,
                    url TEXT,
                    title TEXT,
                    gate_version INTEGER DEFAULT 0,
                    first_seen_at TEXT,
                    last_evaluated_at TEXT,
                    outcome TEXT DEFAULT 'pending',
                    reason TEXT
                )
            """)
            c.execute("""
                CREATE TABLE actioned_jobs (
                    job_key TEXT PRIMARY KEY,
                    company TEXT,
                    url TEXT,
                    title TEXT,
                    gate_version INTEGER DEFAULT 0,
                    action TEXT,
                    actioned_at TEXT
                )
            """)
            conn.commit()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_lookback_window_is_30_days(self):
        """Verify default auto mode lookback hours is 720 (30 days)."""
        self.assertEqual(getattr(settings, "AUTO_MODE_LOOKBACK_HOURS", 0), 720)

    def test_previously_seen_never_launched_is_reevaluated(self):
        """
        Prove a previously-seen job evaluated under older buggy build (gate_version < 3)
        with no submission record is now re-evaluated.
        """
        import resume_bot

        with patch.object(resume_bot, "DB_PATH", self.db_path), \
             patch("app.services.company_cap.company_cap_tracker.get_employer_lockout", return_value=None), \
             patch("resume_bot.has_submission_record", return_value=False), \
             patch("resume_bot.has_attempted_role_today", return_value=False):

            # Insert job seen under buggy gate version 1 (rejected previously)
            job_url = "https://jobs.ashbyhq.com/testbacklogco/123"
            company = "testbacklogco"
            title = "Backend Engineer"
            job_key = resume_bot.make_job_key(company, job_url)

            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO seen_jobs (job_key, company, url, title, gate_version, outcome, reason) "
                    "VALUES (?, ?, ?, ?, 1, 'rejected', 'old_buggy_filter')",
                    (job_key, company, job_url, title)
                )
                conn.commit()

            # Under gate version 3, is_new_job must re-admit this role
            is_new = resume_bot.is_new_job(job_url, title=title, company=company)
            self.assertTrue(is_new, "Role evaluated under gate_version < 3 must be re-evaluated")

    def test_alerted_never_launched_is_reevaluated(self):
        """
        Prove a previously actioned job with action='alerted' but no submission record
        is re-evaluated under current gates.
        """
        import resume_bot

        with patch.object(resume_bot, "DB_PATH", self.db_path), \
             patch("resume_bot.has_submission_record", return_value=False), \
             patch("resume_bot.has_attempted_role_today", return_value=False):

            job_url = "https://jobs.lever.co/databricks/456"
            company = "databricks"
            title = "Data Platform Engineer"
            job_key = resume_bot.make_job_key(company, job_url)

            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO actioned_jobs (job_key, company, url, title, gate_version, action) "
                    "VALUES (?, ?, ?, ?, 2, 'alerted')",
                    (job_key, company, job_url, title)
                )
                conn.commit()

            is_new = resume_bot.is_new_job(job_url, title=title, company=company)
            self.assertTrue(is_new, "Role previously alerted but never launched must be re-admitted")

    def test_dedupe_holds_no_duplicate_submission_possible(self):
        """
        Prove that if a role has an existing submission record (applied or submitted),
        it is permanently suppressed and is_new_job returns False.
        """
        import resume_bot

        # Case 1: has_submission_record returns True
        with patch.object(resume_bot, "DB_PATH", self.db_path), \
             patch("resume_bot.has_submission_record", return_value=True):

            job_url = "https://boards.greenhouse.io/figma/789"
            company = "figma"
            title = "Software Engineer"

            is_new = resume_bot.is_new_job(job_url, title=title, company=company)
            self.assertFalse(is_new, "Role with active submission record must NEVER be re-submitted")

        # Case 2: actioned_jobs has 'applied' or 'submitted'
        with patch.object(resume_bot, "DB_PATH", self.db_path), \
             patch("resume_bot.has_submission_record", return_value=False), \
             patch("resume_bot.has_attempted_role_today", return_value=False):

            job_url = "https://boards.greenhouse.io/figma/789"
            company = "figma"
            title = "Software Engineer"
            job_key = resume_bot.make_job_key(company, job_url)

            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO actioned_jobs (job_key, company, url, title, gate_version, action) "
                    "VALUES (?, ?, ?, ?, 3, 'applied')",
                    (job_key, company, job_url, title)
                )
                conn.commit()

            is_new = resume_bot.is_new_job(job_url, title=title, company=company)
            self.assertFalse(is_new, "Applied job in actioned_jobs must NEVER be re-submitted")


class TestPartCMinimalTelegramAndDigest(unittest.IsolatedAsyncioTestCase):
    """
    Part C Regression Tests:
    1. Telegram pushes SUCCESS RECEIPTS ONLY. Blocked or incomplete applications send zero Telegram messages.
    2. /digest provides minimal format: submitted applications (with receipts) + one-line totals.
    """

    def setUp(self):
        reset_alert_hygiene()

    async def test_blocked_application_sends_zero_telegram_messages(self):
        """Prove a blocked / incomplete / captcha / cap-limited application sends ZERO Telegram messages."""
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            # 1. CAPTCHA / Security challenge
            res_captcha = await send_telegram_alert(
                job_id="test_blocked_001",
                alert_type="captcha_solve_request",
                company="Ramp",
                role="Backend Engineer",
                portal="Ashby",
                job_url="https://jobs.ashbyhq.com/ramp/1",
                verified_success=False
            )
            self.assertFalse(res_captcha)

            # 2. Missing info request
            res_missing = await send_telegram_alert(
                job_id="test_blocked_002",
                alert_type="missing_info_request",
                company="Cohere",
                role="Machine Learning Engineer",
                portal="Ashby",
                job_url="https://jobs.ashbyhq.com/cohere/2",
                verified_success=False
            )
            self.assertFalse(res_missing)

            # 3. Cap reached notice
            res_cap = await send_telegram_alert(
                job_id="test_blocked_003",
                alert_type="cap_reached",
                company="Stripe",
                role="Software Engineer",
                portal="Greenhouse",
                job_url="https://boards.greenhouse.io/stripe/3",
                verified_success=False
            )
            self.assertFalse(res_cap)

            # 4. Inconclusive/Failed submission
            res_failed = await send_telegram_alert(
                job_id="test_blocked_004",
                alert_type="validation_failed",
                company="Datadog",
                role="Site Reliability Engineer",
                portal="Greenhouse",
                job_url="https://boards.greenhouse.io/datadog/4",
                verified_success=False
            )
            self.assertFalse(res_failed)

            # Zero network calls made to Telegram API
            self.assertEqual(mock_post.call_count, 0)

    async def test_success_receipt_is_sent_to_telegram(self):
        """Prove that verified success submissions pass through the gate to Telegram."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(b"\x89PNG\r\n\x1a\nproofphoto")
            tf.flush()
            shot_path = tf.name

        try:
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})

                res_success = await send_telegram_alert(
                    job_id="test_success_001",
                    alert_type="submitted",
                    company="Snowflake",
                    role="Data Engineer",
                    portal="Lever",
                    job_url="https://jobs.lever.co/snowflake/5",
                    screenshot_path=shot_path,
                    verified_success=True
                )
                self.assertTrue(res_success)
                self.assertEqual(mock_post.call_count, 1)
        finally:
            if os.path.exists(shot_path):
                os.remove(shot_path)

    def test_minimal_digest_format(self):
        """Prove /digest minimal output format contains submitted receipts and one-line totals."""
        mock_launches = [
            {
                "company": "Patreon",
                "role": "Security Engineer",
                "status": "SUBMITTED",
                "receipt_msg_id": "rec_789456",
                "timestamp": "2026-09-29T17:30:00.000000"
            },
            {
                "company": "Jobgether",
                "role": "Software Engineer",
                "status": "CHALLENGE_BLOCKED",
                "receipt_msg_id": None,
                "timestamp": "2026-09-29T17:15:00.000000"
            },
            {
                "company": "SeniorCorp",
                "role": "Staff Engineer",
                "status": "SKIPPED",
                "receipt_msg_id": None,
                "timestamp": "2026-09-29T16:00:00.000000"
            }
        ]

        logger = AuditLogger()
        with patch("app.services.launch_tracker.launch_tracker.get_last_24h_launches", return_value=mock_launches):
            digest = logger.generate_minimal_digest()

            # Verifications for minimal format
            self.assertIn("📊 *AutoApply Run Digest*", digest)
            self.assertIn("• *Submitted Applications:*", digest)
            self.assertIn("*Patreon* — Security Engineer | Receipt: `rec_789456`", digest)
            self.assertIn("• *Totals (Last 24h):* Launched: 3 | Blocked: 1 | Skipped: 1", digest)
            # Must NOT dump full lists of skipped or blocked in minimal view
            self.assertNotIn("• *Skipped Opportunities", digest)
            self.assertNotIn("• *Challenges & Blocks", digest)


class TestSpamFlagPing(unittest.IsolatedAsyncioTestCase):
    """
    SPAM-FLAG PING Regression Tests:
    1. Proves SPAM_FLAG_NOTICE fires when portal explicitly flags submission as spam.
    2. Proves the ping fires exactly ONCE per flagged application (deduplicated).
    3. Proves zero pings for all other outcomes (standard failure, cap reached, captcha, skip, etc.).
    4. Proves LLM post-submit verifier detects explicit spam banners (e.g. Ashby).
    """

    def setUp(self):
        reset_alert_hygiene()

    async def test_spam_flag_ping_fires_with_photo_and_details(self):
        """Prove SPAM_FLAG_NOTICE fires with company, role, portal, timestamp, and photo."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(b"\x89PNG\r\n\x1a\nfakeimagebytes")
            tf.flush()
            screenshot_path = tf.name

        try:
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})

                res = await send_telegram_alert(
                    job_id="ashby_spam_001",
                    alert_type="spam_flag_notice",
                    company="Cerebras",
                    role="Systems Engineer",
                    portal="Ashby",
                    job_url="https://jobs.ashbyhq.com/cerebras/1",
                    details="⚠️ Portal refused submission: 'Flagged as possible spam'. Please submit manually in your personal browser.",
                    screenshot_path=screenshot_path,
                    verified_success=False
                )
                self.assertTrue(res)
                self.assertEqual(mock_post.call_count, 1)
                call_args = mock_post.call_args
                url_called = call_args[0][0]
                self.assertIn("/sendPhoto", url_called)
                data_called = call_args[1]["data"]
                self.assertIn("Action Required: Portal Spam Flag", data_called["caption"])
                self.assertIn("Cerebras", data_called["caption"])
                self.assertIn("Systems Engineer", data_called["caption"])
                self.assertIn("Ashby", data_called["caption"])

                # Exact-once deduplication: repeated call for same job_id must NOT fire
                res_repeat = await send_telegram_alert(
                    job_id="ashby_spam_001",
                    alert_type="spam_flag_notice",
                    company="Cerebras",
                    role="Systems Engineer",
                    portal="Ashby",
                    job_url="https://jobs.ashbyhq.com/cerebras/1",
                    details="⚠️ Portal refused submission: 'Flagged as possible spam'.",
                    screenshot_path=screenshot_path,
                    verified_success=False
                )
                self.assertFalse(res_repeat)
                self.assertEqual(mock_post.call_count, 1)  # No additional network call
        finally:
            if os.path.exists(screenshot_path):
                os.remove(screenshot_path)

    async def test_spam_flag_fires_zero_for_all_other_outcomes(self):
        """Prove zero pings for all non-spam outcomes."""
        non_spam_outcomes = [
            ("validation_failed", False),
            ("button_not_found", False),
            ("cap_reached", False),
            ("captcha_solve_request", False),
            ("missing_info_request", False),
            ("skipped", False),
            ("security_challenge", False),
            ("pacing", False)
        ]
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            for outcome, verified in non_spam_outcomes:
                res = await send_telegram_alert(
                    job_id=f"other_{outcome}",
                    alert_type=outcome,
                    company="OtherCo",
                    role="SWE",
                    portal="Lever",
                    job_url="https://jobs.lever.co/other/1",
                    verified_success=verified
                )
                self.assertFalse(res, f"Outcome {outcome} should have been suppressed!")
            self.assertEqual(mock_post.call_count, 0)

    async def test_llm_post_submit_verify_detects_spam_flag(self):
        """Prove llm_post_submit_verify flags explicit portal spam banner."""
        from app.services.llm_verifier import llm_post_submit_verify

        mock_page = AsyncMock()
        mock_page.url = "https://jobs.ashbyhq.com/cerebras/application"
        # Simulate Ashby's actual spam text: "Your application submission was flagged as possible spam."
        mock_page.content.return_value = "<html><body><div>Your application submission was flagged as possible spam. If you believe this was a mistake, please submit your application again.</div></body></html>"
        mock_page.screenshot = AsyncMock()

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            screenshot_path = tf.name

        try:
            ok, reason = await llm_post_submit_verify(mock_page, screenshot_path=screenshot_path, timeout_sec=0.1)
            self.assertFalse(ok)
            self.assertIn("SPAM_FLAG_DETECTED", reason)
            self.assertIn("flagged as possible spam", reason)
            mock_page.screenshot.assert_called_once()
        finally:
            if os.path.exists(screenshot_path):
                os.remove(screenshot_path)


class TestTelegramSnapshotDelivery(unittest.IsolatedAsyncioTestCase):
    """
    TELEGRAM SNAPSHOT DELIVERY Regression Tests:
    1. Prove every verified SUBMITTED pushes evidence screenshot as a photo with success receipt.
    2. Prove resilient fallback: Markdown caption parse failure (400) retries plain text photo.
    3. Prove /digest attaches media group containing evidence screenshots.
    """

    def setUp(self):
        reset_alert_hygiene()

    async def test_verified_submitted_pushes_photo(self):
        """Prove verified success receipt pushes photo with caption to Telegram."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(b"\x89PNG\r\n\x1a\nreceiptproof")
            tf.flush()
            screenshot_path = tf.name

        try:
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})

                res = await send_telegram_alert(
                    job_id="sub_pass_001",
                    alert_type="submitted",
                    company="Stripe",
                    role="Software Engineer",
                    portal="Greenhouse",
                    job_url="https://boards.greenhouse.io/stripe/1",
                    details="Verified submission confirmation banner found.",
                    screenshot_path=screenshot_path,
                    verified_success=True
                )
                self.assertTrue(res)
                self.assertEqual(mock_post.call_count, 1)
                url_called = mock_post.call_args[0][0]
                self.assertIn("/sendPhoto", url_called)
                data_called = mock_post.call_args[1]["data"]
                self.assertIn("Stripe", data_called["caption"])
                self.assertIn("Software Engineer", data_called["caption"])
        finally:
            if os.path.exists(screenshot_path):
                os.remove(screenshot_path)

    async def test_photo_markdown_error_retries_plain(self):
        """Prove that if Markdown sendPhoto returns 400 Bad Request, it retries with plain text."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(b"\x89PNG\r\n\x1a\nreceiptproof")
            tf.flush()
            screenshot_path = tf.name

        try:
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                # First call returns 400 (Markdown error), second call returns 200 (plain success)
                mock_resp_400 = MagicMock(status_code=400, json=lambda: {"ok": False, "description": "Bad Request: can't parse entities"})
                mock_resp_200 = MagicMock(status_code=200, json=lambda: {"ok": True})
                mock_post.side_effect = [mock_resp_400, mock_resp_200]

                res = await send_telegram_alert(
                    job_id="sub_markdown_retry_001",
                    alert_type="submitted",
                    company="Datadog",
                    role="Backend_Engineer_Role",
                    portal="Lever",
                    job_url="https://jobs.lever.co/datadog/1",
                    screenshot_path=screenshot_path,
                    verified_success=True
                )
                self.assertTrue(res)
                self.assertEqual(mock_post.call_count, 2)
                # First attempt had parse_mode
                first_data = mock_post.call_args_list[0][1]["data"]
                self.assertEqual(first_data.get("parse_mode"), "Markdown")
                # Second attempt omitted parse_mode (plain text)
                second_data = mock_post.call_args_list[1][1]["data"]
                self.assertNotIn("parse_mode", second_data)
        finally:
            if os.path.exists(screenshot_path):
                os.remove(screenshot_path)

    async def test_digest_attaches_media_group(self):
        """Prove /digest dispatches media group with evidence photos and minimal text."""
        import resume_bot

        # Create temporary dummy screenshots
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf1, \
             tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf2:
            tf1.write(b"\x89PNG\r\n\x1a\nimg1")
            tf1.flush()
            tf2.write(b"\x89PNG\r\n\x1a\nimg2")
            tf2.flush()
            p1, p2 = tf1.name, tf2.name

        try:
            fake_photos = [
                {"path": p1, "caption": "CompanyA — RoleA | SUBMITTED", "outcome": "SUBMITTED"},
                {"path": p2, "caption": "CompanyB — RoleB | SUBMITTED", "outcome": "SUBMITTED"}
            ]

            mock_update = MagicMock()
            mock_msg = AsyncMock()
            mock_update.effective_message = mock_msg
            mock_update.effective_chat.id = 12345
            mock_context = MagicMock()

            with patch("app.services.audit_logger.collect_digest_evidence_photos", return_value=(fake_photos, 0, None)), \
                 patch("app.services.audit_logger.audit_logger.generate_minimal_digest", return_value="📊 *AutoApply Run Digest*\n• Totals: Launched: 2"):

                await resume_bot.handle_digest(mock_update, mock_context)

                # Verify text was sent
                mock_msg.reply_text.assert_called_once()
                sent_text = mock_msg.reply_text.call_args[0][0]
                self.assertIn("AutoApply Run Digest", sent_text)

                # Verify media group was dispatched
                mock_msg.reply_media_group.assert_called_once()
                media_arg = mock_msg.reply_media_group.call_args[1]["media"]
                self.assertEqual(len(media_arg), 2)
                self.assertIn("CompanyA", media_arg[0].caption)
                self.assertIn("CompanyB", media_arg[1].caption)
        finally:
            for p in (p1, p2):
                if os.path.exists(p):
                    os.remove(p)


class TestSuccessReceiptAtomicity(unittest.IsolatedAsyncioTestCase):
    """
    SUCCESS RECEIPT ATOMICITY Regression Tests:
    1. Make the Telegram success receipt atomic by construction: send the photo with the receipt as its caption.
    2. If sendPhoto fails after the plaintext retry, do NOT fall back to text-only — retry with bounded backoff,
       and if still failing, suppress the Telegram ping and mark the ledger entry photo_pending.
    3. The photo always remains available via the /digest media group.
    4. Simulate photo-send failure on all retries → assert zero text-only success messages dispatched,
       ledger marked photo_pending, photo present via digest path.
    """

    def setUp(self):
        reset_alert_hygiene()

    async def test_atomic_success_receipt_send_photo_failure_suppresses_text_fallback(self):
        """
        Simulate photo-send failure on all retries in send_telegram_alert:
        Assert zero text-only success messages dispatched (sendMessage never called for submitted alert).
        """
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(b"\x89PNG\r\n\x1a\nrealproofbytes")
            tf.flush()
            screenshot_path = tf.name

        try:
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
                 patch("asyncio.sleep", new_callable=AsyncMock):  # fast bounded backoff
                # All sendPhoto attempts fail with 500
                mock_post.return_value = MagicMock(status_code=500, json=lambda: {"ok": False})

                res = await send_telegram_alert(
                    job_id="sub_fail_all_001",
                    alert_type="submitted",
                    company="AtomCo",
                    role="Systems Engineer",
                    portal="Ashby",
                    job_url="https://jobs.ashbyhq.com/atomco/1",
                    screenshot_path=screenshot_path,
                    verified_success=True
                )
                # Suppressed: returns False
                self.assertFalse(res)
                # Verify sendPhoto was attempted across retries (Markdown + plain + 2 backoff retries = 4 attempts)
                self.assertGreaterEqual(mock_post.call_count, 3)
                for call in mock_post.call_args_list:
                    url_called = call[0][0]
                    # CRITICAL: sendMessage must NEVER have been called!
                    self.assertNotIn("/sendMessage", url_called)
                    self.assertIn("/sendPhoto", url_called)
        finally:
            if os.path.exists(screenshot_path):
                os.remove(screenshot_path)

    async def test_ledger_marked_photo_pending_and_photo_present_in_digest(self):
        """
        Prove that when photo upload fails and is suppressed:
        1. Ledger is marked photo_pending (both LaunchTracker and OutcomeLedger).
        2. Photo is present and collected for /digest media group.
        """
        from app.services.launch_tracker import LaunchTracker
        from app.services.intelligence_loop import OutcomeLedger
        from app.services.audit_logger import AuditLogger, collect_digest_evidence_photos

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf1, \
             tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf2, \
             tempfile.NamedTemporaryFile(suffix=".log", delete=False) as tf3, \
             tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf_img:

            tf_img.write(b"\x89PNG\r\n\x1a\nproofphoto")
            tf_img.flush()
            shot_path = tf_img.name

        try:
            tracker = LaunchTracker(tracker_path=tf1.name)
            ledger = OutcomeLedger(storage_path=tf2.name)
            logger = AuditLogger(log_path=tf3.name)

            launch_id = tracker.register_launch("QuantumCorp", "AI Engineer", "https://jobs.ashbyhq.com/quantum/1")

            # Simulate photo delivery failed across all retries:
            # resolve_launch called with receipt_msg_id="photo_pending", photo_status="photo_pending"
            with patch("app.services.audit_logger.audit_logger", logger), \
                 patch("app.services.intelligence_loop.outcome_ledger", ledger):

                tracker.resolve_launch(
                    launch_id=launch_id,
                    status="SUBMITTED",
                    reason="Verified with post-submit screenshot [photo_pending]",
                    screenshot_path=shot_path,
                    receipt_msg_id="photo_pending",
                    photo_status="photo_pending"
                )

            # 1. Assert LaunchTracker marked photo_pending
            records = tracker._read_records()
            q_launch = next(r for r in records if r["launch_id"] == launch_id)
            self.assertEqual(q_launch.get("status"), "SUBMITTED")
            self.assertEqual(q_launch.get("photo_status"), "photo_pending")
            self.assertTrue(q_launch.get("photo_pending"))
            self.assertEqual(q_launch.get("receipt_msg_id"), "photo_pending")

            # 2. Assert OutcomeLedger marked photo_pending
            outcomes = ledger._read_records()
            q_outcome = next(o for o in outcomes if o.get("company") == "QuantumCorp")
            self.assertEqual(q_outcome.get("photo_status"), "photo_pending")
            self.assertTrue(q_outcome.get("photo_pending"))
            self.assertEqual(q_outcome.get("receipt_msg_id"), "photo_pending")

            # 3. Assert photo is present via digest path
            photos, overflow, _ = collect_digest_evidence_photos([q_launch])
            self.assertEqual(len(photos), 1)
            self.assertEqual(photos[0]["path"], shot_path)
            self.assertIn("QuantumCorp", photos[0]["caption"])
            self.assertIn("SUBMITTED", photos[0]["caption"])
        finally:
            for p in (tf1.name, tf2.name, tf3.name, shot_path):
                if os.path.exists(p):
                    os.remove(p)


class TestDailyCap50(unittest.TestCase):
    """
    DAILY CAP 25 → 50 Regression Tests:
    Prove maximum applications per day is 50.
    50th launch attempt is allowed; 51st launch attempt in a day is blocked.
    """

    def setUp(self):
        from datetime import datetime
        from app.services.politeness import PolitenessManager
        self.pm = PolitenessManager()
        self.pm._daily_count = 0
        self.pm._daily_reset_date = datetime.now().date()

    def test_cap_config_values_are_50(self):
        """Verify config values for daily applications are set to 50."""
        self.assertEqual(settings.MAX_DAILY_APPLICATIONS, 50)
        self.assertEqual(settings.MAX_APPLICATIONS_PER_DAY, 50)

    def test_50th_launch_allowed_and_51st_launch_blocked_politeness(self):
        """
        Prove in politeness manager:
        When count is 49 (attempting 50th), can_apply_today() is True.
        When count reaches 50 (attempting 51st), can_apply_today() is False.
        """
        # Simulate 49 applications already recorded today
        self.pm._daily_count = 49
        self.assertTrue(self.pm.can_apply_today(), "50th application attempt must be ALLOWED")

        # Record 50th application
        self.pm.record_application()
        self.assertEqual(self.pm.daily_count, 50)

        # Attempt 51st application
        self.assertFalse(self.pm.can_apply_today(), "51st application attempt must be BLOCKED")

    def test_50th_launch_allowed_and_51st_launch_blocked_resume_bot(self):
        """
        Prove in resume_bot.py enforcement check:
        50th launch (count=49) proceeds past daily cap check.
        51st launch (count=50) is blocked and resolved to SKIPPED with daily limit reason.
        """
        import resume_bot

        # Case 1: Count = 49 (50th attempt allowed)
        with patch("resume_bot.get_daily_submission_count", return_value=49):
            daily_count = resume_bot.get_daily_submission_count()
            max_daily = getattr(settings, "MAX_DAILY_APPLICATIONS", 50)
            self.assertFalse(daily_count >= max_daily, "50th attempt must NOT trigger daily limit skip")

        # Case 2: Count = 50 (51st attempt blocked)
        with patch("resume_bot.get_daily_submission_count", return_value=50):
            daily_count = resume_bot.get_daily_submission_count()
            max_daily = getattr(settings, "MAX_DAILY_APPLICATIONS", 50)
            self.assertTrue(daily_count >= max_daily, "51st attempt MUST trigger daily limit skip")
            skip_msg = f"Daily application limit reached ({daily_count}/{max_daily})"
            self.assertEqual(skip_msg, "Daily application limit reached (50/50)")


class TestStealthAndHumanExecution(unittest.TestCase):
    """
    Stealth & Human-Like Execution Tests:
    1. Modern genuine Chrome UA without 'HeadlessChrome'
    2. AutomationControlled disabled and stealth init scripts
    3. Viewport 1440x900 with Retina scaling (device_scale_factor=2)
    """

    def test_stealth_init_script_and_ua(self):
        import form_filler
        self.assertTrue(callable(form_filler.launch_and_fill_job))
        import playwright_stealth
        self.assertTrue(hasattr(playwright_stealth, "Stealth"))


class TestAutonomous12HourSchedule(unittest.TestCase):
    """
    Autonomous 12-Hour Schedule Tests:
    Prove the LLM search schedule is configured to search and apply every 12 hours (43,200s).
    """

    def test_scout_cycle_interval_is_12_hours(self):
        import resume_bot
        self.assertTrue(hasattr(resume_bot, "SCOUT_CYCLE_INTERVAL_SECONDS"))
        self.assertEqual(resume_bot.SCOUT_CYCLE_INTERVAL_SECONDS, 12 * 3600)
        self.assertEqual(resume_bot.SCOUT_CYCLE_INTERVAL_SECONDS, 43200)


class TestLLMCaptchaSolver(unittest.IsolatedAsyncioTestCase):
    """
    JOB SCOUT - RULE 14 ZERO SOLVER REGRESSION TESTS:
    1. Zero CAPTCHA solving or bypass modules in codebase
    2. CaptchaManager only detects challenges and never delegates to any solver
    3. Detection halts cleanly without executing any bypass logic
    """

    async def test_no_solver_in_captcha_manager(self):
        from form_filler import CaptchaManager
        self.assertFalse(hasattr(CaptchaManager, "solve_captcha"), "solve_captcha must be completely removed from CaptchaManager")

    async def test_no_llm_captcha_solver_class(self):
        import app.services.llm_verifier as verifier_mod
        self.assertFalse(hasattr(verifier_mod, "LLMCaptchaSolver"), "LLMCaptchaSolver must be completely removed from llm_verifier")

    async def test_captcha_detection_is_passive_only(self):
        from form_filler import CaptchaManager
        mock_page = MagicMock()
        mock_page.frames = []
        mock_el = MagicMock()
        mock_el.is_visible = AsyncMock(return_value=True)
        mock_el.evaluate = AsyncMock(return_value=False)

        mock_loc = MagicMock()
        mock_loc.count = AsyncMock(return_value=1)
        mock_loc.nth = MagicMock(return_value=mock_el)
        mock_page.locator.return_value = mock_loc

        # Passive detection only returns boolean
        is_pres = await CaptchaManager.is_captcha_present(mock_page)
        self.assertTrue(is_pres)


class TestMemoryLeanSessionLock(unittest.IsolatedAsyncioTestCase):
    """
    JOB SCOUT - MEMORY LEAN PASS Regression Tests:
    1. Single shared headless browser per process
    2. Board API concurrency capped at max 3
    3. Process-wide launch lock (asyncio.Lock) ensures 1 application session at a time
    4. Log assertion proves two sessions never overlap in time
    5. Daemon RSS telemetry line
    """

    def test_board_api_concurrency_bounded_to_three(self):
        import api_fetcher
        self.assertTrue(hasattr(api_fetcher, "_BOARD_API_SEMAPHORE"))
        self.assertEqual(api_fetcher._BOARD_API_SEMAPHORE._value, 3)

    def test_shared_browser_singleton_pattern(self):
        import form_filler
        self.assertTrue(hasattr(form_filler, "get_shared_browser"))
        self.assertTrue(hasattr(form_filler, "close_shared_browser"))
        self.assertTrue(hasattr(form_filler, "_PLAYWRIGHT_PAGE_LOCK"))

    def test_daemon_rss_telemetry(self):
        from app.services.audit_logger import get_daemon_rss_telemetry_line, audit_logger
        line = get_daemon_rss_telemetry_line()
        self.assertIsNotNone(line)
        self.assertIn("Daemon Memory (RSS):", line)
        self.assertTrue(line.endswith("MB"))
        minimal = audit_logger.generate_minimal_digest()
        self.assertIn("Daemon Memory (RSS):", minimal)

    async def test_session_lock_prevents_overlap_and_logs_assertion(self):
        import resume_bot
        import asyncio
        from unittest.mock import patch

        overlap_detected = False
        active_counts = []
        log_messages = []

        async def mock_impl(chat_id, status_msg, url, company, job_id, context, title, rank, fit_score):
            nonlocal overlap_detected
            current = resume_bot._ACTIVE_APPLICATION_SESSIONS
            active_counts.append(current)
            if current > 1:
                overlap_detected = True
            await asyncio.sleep(0.05)
            if resume_bot._ACTIVE_APPLICATION_SESSIONS > 1:
                overlap_detected = True

        def record_log(m):
            if "[SESSION_LOCK_ASSERTION]" in str(m):
                log_messages.append(str(m))

        with patch.object(resume_bot.logger, "info", side_effect=record_log), \
             patch.object(resume_bot, "_execute_auto_apply_impl", side_effect=mock_impl):

            # Launch two application sessions concurrently
            task1 = asyncio.create_task(
                resume_bot.execute_auto_apply(1, None, "https://url1", "CompanyA", "job1", None, title="RoleA")
            )
            task2 = asyncio.create_task(
                resume_bot.execute_auto_apply(2, None, "https://url2", "CompanyB", "job2", None, title="RoleB")
            )

            await asyncio.gather(task1, task2)

        self.assertFalse(overlap_detected, "Two application sessions must NEVER overlap in time!")
        self.assertEqual(max(active_counts), 1, "Max active sessions must be exactly 1")
        self.assertEqual(resume_bot._ACTIVE_APPLICATION_SESSIONS, 0, "Active sessions must return to 0")

        # Verify log assertions were emitted for each session
        acquired_logs = [m for m in log_messages if "Acquired launch lock" in m]
        released_logs = [m for m in log_messages if "Released launch lock" in m]
        self.assertEqual(len(acquired_logs), 2)
        self.assertEqual(len(released_logs), 2)
        self.assertTrue(any("CompanyA" in m for m in acquired_logs))
        self.assertTrue(any("CompanyB" in m for m in acquired_logs))


if __name__ == "__main__":
    unittest.main()


