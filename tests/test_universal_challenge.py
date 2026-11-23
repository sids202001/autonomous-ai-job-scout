"""
Unit and Integration Tests for Universal Security-Challenge Handling Pipeline
Validates:
1. CHALLENGE_SIGNATURES registry covers all portals and challenge families:
   - Google reCAPTCHA
   - hCaptcha
   - Arkose Labs / FunCaptcha
   - Cloudflare Turnstile & Managed Challenge
   - DataDome
   - PerimeterX / HUMAN Security
   - Kasada
   - AWS WAF Captcha
   - Generic Verification Markers
2. Silent on clean forms.
3. Pre-fill stage clean halt: zero interactive solve waits, context closed immediately.
4. Mid-fill stage clean halt: stopped during form progression, context closed immediately.
5. Post-submit interstitial detection: ~15s watcher catches challenges mounted after submit click.
6. Zero Telegram noise on challenge halts (all stages).
7. Submissions log & End-of-run digest output under 'Manual-only: security challenge'.
"""
import os
import tempfile
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

from app.services.presubmit_gate import (
    CHALLENGE_SIGNATURES,
    SecurityChallengeDetector,
)
from app.services.audit_logger import AuditLogger
from app.services.auto_filler import AutoFiller, verify_submission
from app.services.telegram_notifier import send_telegram_alert, reset_alert_hygiene
from app.services.code_fetcher import send_telegram_code_request


class Test1ChallengeSignaturesRegistry(unittest.TestCase):
    """Verifies that CHALLENGE_SIGNATURES correctly detects all anti-bot challenge fixtures."""

    def test_detects_google_recaptcha_fixtures(self):
        # Class fixture
        html1 = '<div class="g-recaptcha" data-sitekey="6Ld-xyz"></div>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html1)
        self.assertTrue(detected)
        self.assertIn("recaptcha", reason.lower())

        # Iframe fixture
        html2 = '<iframe src="https://www.google.com/recaptcha/api2/anchor?k=6Ld-xyz"></iframe>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html2)
        self.assertTrue(detected)
        self.assertIn("recaptcha", reason.lower())

    def test_detects_hcaptcha_fixtures(self):
        # Class fixture
        html1 = '<div class="h-captcha" data-sitekey="xyz"></div>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html1)
        self.assertTrue(detected)
        self.assertIn("h-captcha", reason.lower())

        # Iframe fixture
        html2 = '<iframe src="https://newassets.hcaptcha.com/captcha/v1/xyz"></iframe>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html2)
        self.assertTrue(detected)
        self.assertIn("h-captcha", reason.lower())

    def test_detects_arkose_funcaptcha_fixtures(self):
        # Arkoselabs iframe
        html1 = '<iframe src="https://client-api.arkoselabs.com/fc/api/"></iframe>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html1)
        self.assertTrue(detected)
        self.assertIn("arkose", reason.lower())

        # FunCaptcha iframe
        html2 = '<iframe src="https://funcaptcha.com/fc/api/"></iframe>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html2)
        self.assertTrue(detected)
        self.assertIn("arkose", reason.lower())

    def test_detects_cloudflare_turnstile_and_managed_challenge(self):
        # Turnstile iframe
        html1 = '<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/"></iframe>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html1)
        self.assertTrue(detected)
        self.assertIn("cloudflare", reason.lower())

        # Turnstile wrapper div
        html2 = '<div class="cf-turnstile" data-sitekey="0x4AAAAAA"></div>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html2)
        self.assertTrue(detected)
        self.assertIn("cloudflare", reason.lower())

        # Interstitial text
        html3 = '<p>Checking your browser before accessing the site...</p>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html3)
        self.assertTrue(detected)
        self.assertIn("cloudflare", reason.lower())

    def test_detects_datadome_fixtures(self):
        # DataDome captcha iframe
        html1 = '<iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=AHrlqAAAAAMA"></iframe>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html1)
        self.assertTrue(detected)
        self.assertIn("datadome", reason.lower())

        # DataDome captcha container id
        html2 = '<div id="datadome-captcha"></div>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html2)
        self.assertTrue(detected)
        self.assertIn("datadome", reason.lower())

    def test_detects_perimeterx_human_security_fixtures(self):
        # PerimeterX captcha element
        html1 = '<div id="px-captcha"></div>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html1)
        self.assertTrue(detected)
        self.assertIn("perimeterx", reason.lower())

        # Human security text
        html2 = '<div>Press & hold to confirm you are a human</div>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html2)
        self.assertTrue(detected)
        self.assertIn("perimeterx", reason.lower())

    def test_detects_kasada_fixtures(self):
        # Kasada div
        html = '<div id="kasada-enforcement"></div>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html)
        self.assertTrue(detected)
        self.assertIn("kasada", reason.lower())

    def test_detects_aws_waf_captcha_fixtures(self):
        # AWS WAF container id
        html1 = '<div id="aws-waf-captcha"></div>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html1)
        self.assertTrue(detected)
        self.assertIn("aws waf", reason.lower())

        # AWS WAF token iframe
        html2 = '<iframe src="https://token.awswaf.com/captcha/token"></iframe>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html2)
        self.assertTrue(detected)
        self.assertIn("aws waf", reason.lower())

    def test_detects_generic_security_check(self):
        html1 = '<h2>Please verify you are human to proceed</h2>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html1)
        self.assertTrue(detected)
        self.assertIn("verify", reason.lower())

        html2 = '<div data-qa="captcha-container">Security Check</div>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html2)
        self.assertTrue(detected)

    def test_silent_on_clean_form(self):
        html = '''
        <html>
            <body>
                <form id="job-app">
                    <label for="name">Full Name</label>
                    <input type="text" id="name" name="name" value="Siddhesh Santosh Sawant" />
                    <button type="submit">Submit Application</button>
                </form>
            </body>
        </html>
        '''
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html)
        self.assertFalse(detected)
        self.assertIsNone(reason)


class Test2PreFillStageHalt(unittest.IsolatedAsyncioTestCase):
    """Verifies that pre-fill detection cleanly halts without interactive solve delays."""

    async def test_pre_fill_challenge_closes_context_immediately(self):
        filler = AutoFiller()
        mock_page = AsyncMock()
        mock_context = AsyncMock()

        # Mock page content containing Cloudflare challenge
        mock_page.content = AsyncMock(return_value='<html><body>Checking your browser before accessing...</body></html>')
        mock_page.locator.return_value.count = AsyncMock(return_value=0)
        mock_page.locator.return_value.first.count = AsyncMock(return_value=0)
        mock_page.frames = []

        # Mock browser launch to return our mock page and context
        mock_playwright = AsyncMock()
        mock_browser = AsyncMock()
        mock_browser.new_context.return_value = mock_context
        mock_context.new_page.return_value = mock_page
        mock_playwright.chromium.launch.return_value = mock_browser
        mock_playwright.chromium.launch_persistent_context.return_value = mock_context

        with patch("playwright.async_api.async_playwright") as mock_ap, \
             patch.object(filler, "human_delay", new_callable=AsyncMock):
            mock_ap.return_value.__aenter__.return_value = mock_playwright

            profile = MagicMock()
            profile.first_name = "Siddhesh"
            profile.last_name = "Sawant"

            res = await filler.open_and_prefill(
                job_url="https://jobs.ashbyhq.com/ramp/software-engineer",
                profile=profile,
                mode="review"  # Even in review mode, must NOT hang for 300,000ms
            )

            # Assert immediate clean halt
            self.assertEqual(res["status"], "security_challenge")
            self.assertEqual(res["challenge_stage"], "pre_fill")
            self.assertTrue(res["captcha_detected"])
            # Context must be closed
            mock_context.close.assert_awaited()
            # Must NOT have called wait_for_timeout(300000)
            for call in mock_page.wait_for_timeout.await_args_list:
                self.assertNotEqual(call.args[0], 300000)


class Test3MidFillStageHalt(unittest.IsolatedAsyncioTestCase):
    """Verifies that mid-fill challenge detection cleanly halts multi-step progression."""

    async def test_mid_fill_challenge_halts_progression(self):
        filler = AutoFiller()
        mock_page = AsyncMock()

        # Step 1: Clean page
        # Step 2: Injected challenge
        call_count = 0
        async def mock_content():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                return '<html><body><div class="h-captcha"></div></body></html>'
            return '<html><body><input type="text" name="city" /></body></html>'

        mock_page.content = mock_content
        mock_page.locator.return_value.count = AsyncMock(return_value=0)
        mock_page.locator.return_value.first.count = AsyncMock(return_value=0)
        mock_page.frames = []

        profile = MagicMock()
        with patch.object(filler, "fill_form_fields", new_callable=AsyncMock) as mock_fill, \
             patch.object(filler, "safe_click", new_callable=AsyncMock) as mock_click, \
             patch.object(filler, "human_delay", new_callable=AsyncMock):

            mock_fill.return_value = {"city": "Washington"}
            mock_click.return_value = True  # clicked next

            fields = await filler.navigate_multi_step(mock_page, profile, max_steps=5)

            # Mid-fill detection caught the challenge
            self.assertIn("_challenge_detected", fields)
            self.assertEqual(fields["_challenge_stage"], "mid_fill")


class Test4PostSubmitStageHalt(unittest.IsolatedAsyncioTestCase):
    """Verifies post-submit interstitial monitoring (~15s) catches late-mounting challenges."""

    async def test_post_submit_challenge_polling_detects_interstitial(self):
        mock_page = AsyncMock()

        # Simulate Ramp/Waabi Ashby submission:
        # T=0 to T=1s: submitting...
        # T=2s: Cloudflare Turnstile / security challenge appears
        ticks = 0
        async def mock_content():
            nonlocal ticks
            ticks += 1
            if ticks >= 2:
                return '<html><body><div id="cf-challenge">Checking your browser before accessing</div></body></html>'
            return '<html><body>Submitting application...</body></html>'

        mock_page.content = mock_content
        mock_page.locator.return_value.count = AsyncMock(return_value=0)
        mock_page.locator.return_value.first.count = AsyncMock(return_value=0)
        mock_page.locator.return_value.first.is_visible = AsyncMock(return_value=False)
        mock_page.frames = []
        mock_page.url = "https://jobs.ashbyhq.com/ramp/apply"

        verified, reason = await verify_submission(mock_page, timeout_sec=5.0)

        self.assertFalse(verified)
        self.assertIn("security challenge detected", reason.lower())


class Test5AuditLoggerAndRunDigest(unittest.TestCase):
    """Verifies submissions.log logging and end-of-run digest formatting for security challenges."""

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

    def test_challenge_logged_to_file_and_surfaced_in_digest(self):
        self.audit.reset_run()

        # Record 1 verified submitted
        self.audit.log_submission(
            company="Stripe",
            role_title="Backend Engineer",
            job_url="https://jobs.lever.co/stripe/backend-eng",
            status="submitted",
            match_reason="Python/Distributed Systems Match"
        )

        # Record 1 security challenge (Ramp Ashby post-submit challenge)
        self.audit.log_submission(
            company="Ramp",
            role_title="Software Engineer",
            job_url="https://jobs.ashbyhq.com/ramp/swe",
            status="security_challenge",
            skip_reason="Post-submit security challenge detected: Cloudflare Turnstile",
            portal="ashby",
            challenge_type="Cloudflare Turnstile",
            stage_detected="post_submit"
        )

        # Record 1 security challenge (Waabi mid-fill challenge)
        self.audit.log_submission(
            company="Waabi",
            role_title="Robotics Software Engineer",
            job_url="https://jobs.ashbyhq.com/waabi/robotics",
            status="security_challenge",
            skip_reason="Security challenge detected during form fill: h-Captcha",
            portal="ashby",
            challenge_type="h-Captcha",
            stage_detected="mid_fill"
        )

        # Record 1 regular skip
        self.audit.log_submission(
            company="Figma",
            role_title="Product Engineer",
            job_url="https://jobs.lever.co/figma/prod-eng",
            status="skipped",
            skip_reason="Missing required compliance field"
        )

        digest = self.audit.generate_run_digest()

        # Assert summary counts
        self.assertIn("*Submitted Applications:* 1", digest)
        self.assertIn("*Skipped / Blocked Roles:* 3", digest)

        # Assert Manual-only security challenge section
        self.assertIn("⚠️ *Manual-only: security challenge:*", digest)
        self.assertIn("• *Ramp* — Software Engineer (Cloudflare Turnstile, post_submit)", digest)
        self.assertIn("↳ Direct URL: https://jobs.ashbyhq.com/ramp/swe", digest)
        self.assertIn("• *Waabi* — Robotics Software Engineer (h-Captcha, mid_fill)", digest)
        self.assertIn("↳ Direct URL: https://jobs.ashbyhq.com/waabi/robotics", digest)

        # Assert regular skips
        self.assertIn("⏭ *Skipped / Filtered:*", digest)
        self.assertIn("• *Figma* — Product Engineer", digest)

        # Verify disk log was written with SECURITY_CHALLENGE
        with open(self.tmp_log.name, "r") as f:
            content = f.read()
            self.assertIn("SUBMITTED | Stripe", content)
            self.assertIn("SECURITY_CHALLENGE | Ramp", content)
            self.assertIn("SECURITY_CHALLENGE | Waabi", content)
            self.assertIn("SKIPPED | Figma", content)
            self.assertIn("[Cloudflare Turnstile | stage=post_submit | portal=ashby]", content)


class Test6TelegramStrictNoiseElimination(unittest.IsolatedAsyncioTestCase):
    """Verifies that ZERO Telegram pings are dispatched on challenge halts or code requests."""

    def setUp(self):
        reset_alert_hygiene()

    async def test_zero_telegram_dispatched_on_challenge_and_skips(self):
        with patch("config.settings.TELEGRAM_BOT_TOKEN", "mock_token"), \
             patch("config.settings.TELEGRAM_CHAT_ID", "mock_chat"), \
             patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:

            mock_post.return_value.status_code = 200

            # 1. Dispatch security challenge alert -> MUST BE PERMANENTLY SUPPRESSED
            sent_sec = await send_telegram_alert(
                job_id="job_ramp_chal",
                alert_type="security_challenge",
                company="Ramp",
                role="Software Engineer",
                portal="Ashby",
                job_url="https://jobs.ashbyhq.com/ramp/swe",
                details="Cloudflare Turnstile detected"
            )
            self.assertFalse(sent_sec)

            # 2. Dispatch captcha alert -> MUST BE PERMANENTLY SUPPRESSED
            sent_cap = await send_telegram_alert(
                job_id="job_waabi_chal",
                alert_type="captcha",
                company="Waabi",
                role="Software Engineer",
                portal="Ashby",
                job_url="https://jobs.ashbyhq.com/waabi/swe",
                details="h-Captcha detected"
            )
            self.assertFalse(sent_cap)

            # 3. Verification code request fallback -> MUST BE SUPPRESSED
            await send_telegram_code_request("Ashby", "IMAP timeout")

            # 4. AutoFiller send_telegram_receipt on security_challenge status -> MUST BE SUPPRESSED
            filler = AutoFiller()
            filler.send_telegram_receipt(
                url="https://jobs.ashbyhq.com/ramp/swe",
                company="Ramp",
                timestamp="2026-09-17 12:00 UTC",
                screenshot_path="/tmp/mock.png",
                fields_count=5,
                status="security_challenge",
                reasons=["Cloudflare Turnstile"]
            )

            # Assert ZERO HTTP post requests made to Telegram API across all above
            mock_post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
