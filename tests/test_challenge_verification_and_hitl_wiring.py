"""
Unit tests & regression tests for CHALLENGE-BLOCK VERIFICATION:
1. Negative test cases: Invisible reCAPTCHA badges (Greenhouse/Appian) -> NO challenge detected (false detection eliminated).
2. Positive test cases: Real human-solvable challenge widgets (Arkose Labs / Lever / FunCaptcha, reCAPTCHA bframe, Cloudflare Turnstile) -> Detected.
3. HITL A wiring: Real challenge widget -> triggers CAPTCHA_SOLVE_REQUEST to Telegram (never silent terminal abort without HITL).
4. HITL B wiring: Pre-submit audit unfilled required fields (Writer / Exploration / Sierra) -> routes to MISSING_INFO_REQUEST (never terminal FILTERED).
5. Genuine anti-bot wall: Page-level wall with no widget -> confirms terminal handling (CHALLENGE_BLOCKED, digest-only, zero Telegram dispatch).
"""
import os
import unittest
from unittest.mock import AsyncMock, patch

from app.services.presubmit_gate import SecurityChallengeDetector
from form_filler import CaptchaManager
from app.services.auto_filler import hitl_manager
from app.services.telegram_notifier import send_telegram_alert, reset_alert_hygiene


class TestChallengeVerificationAndHITLWiring(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        reset_alert_hygiene()

    def test_negative_case_greenhouse_invisible_recaptcha_no_challenge(self):
        """
        NEGATIVE TEST CASE:
        Greenhouse embed job application containing invisible reCAPTCHA Enterprise badge.
        Must evaluate to False (NO challenge detected), eliminating the false detections
        that previously killed 7 Greenhouse roles (Appian, Klaviyocampus, etc.).
        """
        # Snapshot mimicking Greenhouse embed DOM with invisible reCAPTCHA enterprise badge
        html_greenhouse = '''
        <html>
        <body>
            <div id="app">
                <form id="application_form">
                    <input name="first_name" value="Siddhesh" />
                    <input name="last_name" value="Sawant" />
                    <input name="email" value="siddhesh@example.com" />
                    <button type="submit">Submit application</button>
                </form>
                <div class="grecaptcha-badge">
                    <div class="grecaptcha-logo">
                        <iframe src="https://www.recaptcha.net/recaptcha/enterprise/anchor?ar=1&k=6LfmcbcpAAAAAChNTbhUShzUOAMj_wY9LQIvLFX0&size=invisible"></iframe>
                    </div>
                </div>
            </div>
        </body>
        </html>
        '''
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html_greenhouse)
        self.assertFalse(detected, f"Invisible reCAPTCHA badge must not be detected as challenge, got: {reason}")
        self.assertIsNone(reason)

    def test_positive_cases_real_challenge_widgets_detected(self):
        """
        POSITIVE TEST CASES:
        Real human-solvable challenge widgets must be detected across all ATS platforms:
        - Arkose Labs / FunCaptcha (Lever - Jobgether & Datalabusa)
        - Google reCAPTCHA interactive bframe puzzle
        - Cloudflare Turnstile
        """
        # Arkose Labs / FunCaptcha fixture (Jobgether/Datalabusa on Lever)
        html_arkose = '<iframe src="https://client-api.arkoselabs.com/fc/api/?token=xyz"></iframe>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html_arkose)
        self.assertTrue(detected)
        self.assertIn("arkose", reason.lower())

        # reCAPTCHA interactive bframe popup
        html_bframe = '<iframe src="https://www.google.com/recaptcha/api2/bframe?cid=123"></iframe>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html_bframe)
        self.assertTrue(detected)
        self.assertIn("recaptcha", reason.lower())

        # Cloudflare Turnstile
        html_turnstile = '<div class="cf-turnstile" data-sitekey="0x4AAAAAA"></div>'
        detected, reason = SecurityChallengeDetector.detect_in_html_snapshot(html_turnstile)
        self.assertTrue(detected)
        self.assertIn("cloudflare", reason.lower())

    async def test_hitl_a_real_challenge_dispatches_captcha_solve_request_to_telegram(self):
        """
        PROVE CODE PATH: Real pre-submit or post-submit widget engages HITL A.
        Dispatches CAPTCHA_SOLVE_REQUEST with Solved/Abandon buttons to Telegram.
        Never executes a terminal abort without HITL.
        """
        with patch("app.services.telegram_notifier.send_telegram_alert", new_callable=AsyncMock) as mock_alert:
            mock_alert.return_value = True

            job_id = "test_job_arkose_101"
            event = await hitl_manager.request_captcha_solve(
                job_id=job_id,
                company="Jobgether",
                role="MLOps Field Engineer",
                portal="Lever",
                job_url="https://jobs.lever.co/jobgether/123",
                screenshot_path="/Users/siddheshsawant/Desktop/screenshots/Jobgether_20260925_232621.png"
            )

            mock_alert.assert_called_once()
            call_kwargs = mock_alert.call_args[1]
            self.assertEqual(call_kwargs["alert_type"], "captcha_solve_request")
            self.assertEqual(call_kwargs["company"], "Jobgether")
            self.assertIn("buttons", call_kwargs)
            buttons = call_kwargs["buttons"]
            # Verify Solved & Abandon buttons are present
            btn_texts = [b["text"] for row in buttons for b in row]
            self.assertTrue(any("Solved" in t for t in btn_texts))
            self.assertTrue(any("Abandon" in t for t in btn_texts))

            # Simulate candidate solving challenge
            resolved = hitl_manager.resolve_hitl_action(job_id, action="solved")
            self.assertTrue(resolved)
            action, data = await hitl_manager.wait_for_resolution(job_id, timeout_sec=5.0)
            self.assertEqual(action, "solved")

    async def test_hitl_b_presubmit_audit_unfilled_field_dispatches_missing_info_request(self):
        """
        PROVE CODE PATH: Pre-submit audit 'unfilled required field' verdicts (Writer/Exploration/Sierra)
        must route into MISSING_INFO_REQUEST, not terminal FILTERED.
        """
        with patch("app.services.telegram_notifier.send_telegram_alert", new_callable=AsyncMock) as mock_alert:
            mock_alert.return_value = True

            # Writer verdict: "Pre-Submit Verification Flagged: Required selection 'Are you located within 50 miles of one of the following office hubs' is not selected"
            job_id = "test_job_writer_202"
            missing_fields = ["Are you located within 50 miles of one of the following office hubs"]

            event = await hitl_manager.request_missing_info(
                job_id=job_id,
                company="Writer",
                role="Software engineer, agents",
                portal="Ashby",
                job_url="https://jobs.ashbyhq.com/writer/123",
                missing_fields=missing_fields,
                screenshot_path="data/screenshots/Writer_presubmit_20260926_090118.png"
            )

            mock_alert.assert_called_once()
            call_kwargs = mock_alert.call_args[1]
            self.assertEqual(call_kwargs["alert_type"], "missing_info_request")
            self.assertEqual(call_kwargs["company"], "Writer")
            self.assertIn("Required field(s) with no resolvable value", call_kwargs["details"])

            # Simulate candidate replying with answer
            resolved = hitl_manager.resolve_hitl_action(job_id, action="info_reply", data="Yes")
            self.assertTrue(resolved)
            action, data = await hitl_manager.wait_for_resolution(job_id, timeout_sec=5.0)
            self.assertEqual(action, "info_reply")
            self.assertEqual(data, "Yes")

    async def test_genuine_antibot_wall_terminal_digest_only_zero_telegram(self):
        """
        PROVE CODE PATH: Genuine anti-bot walls with NO challenge widget (e.g. Cloudflare 403
        Access Denied) terminate cleanly with CHALLENGE_BLOCKED in digest only, zero Telegram dispatch.
        """
        # In telegram_notifier.py, 'security_challenge' and 'blocked' without HITL are permanently suppressed
        alert_sent = await send_telegram_alert(
            job_id="job_wall_303",
            alert_type="security_challenge",
            company="PerimeterWallCo",
            role="Security Engineer",
            portal="Direct",
            job_url="https://example.com/job",
            details="Access Denied - 1020 Ray ID"
        )
        self.assertFalse(alert_sent, "Genuine anti-bot walls must be suppressed from Telegram (digest-only).")


if __name__ == "__main__":
    unittest.main()
