"""
Unit tests covering the 4 bug fixes in sid_job_scrip and connected automation engines:
1. Bug 1: Deterministic policy-based pacing (FIELD_FILL_GAP_MS)
2. Bug 2: Submission verification gate & fresh screenshot handling
3. Bug 3: clear_lever_context (clearing cookies/storage without touching DB)
4. Bug 4: Headless default with DEBUG_HEADED override
"""
import unittest
import os
import time
import tempfile
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
import sys

SCRIP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIP_DIR not in sys.path:
    sys.path.insert(0, SCRIP_DIR)

import config
from app.services.auto_filler import (
    AutoFiller,
    clear_lever_context,
    verify_submission,
)


class TestBug1PolicyBasedPacing(unittest.TestCase):
    """Bug 1: Deterministic policy-based pacing between field interactions."""

    def test_field_fill_gap_ms_config_default(self):
        """Verify default inter-field delay is 600ms."""
        self.assertEqual(config.settings.FIELD_FILL_GAP_MS, 600)

    def test_field_fill_gap_ms_env_override(self):
        """Verify FIELD_FILL_GAP_MS can be tuned via env var."""
        with patch.dict(os.environ, {"FIELD_FILL_GAP_MS": "750"}):
            new_settings = config.Settings()
            self.assertEqual(new_settings.FIELD_FILL_GAP_MS, 750)

    def test_deterministic_field_pacing_call(self):
        """Verify field_pacing uses fixed duration and waits for stable state."""
        filler = AutoFiller()
        mock_el = AsyncMock()
        mock_el.wait_for = AsyncMock()

        start = time.perf_counter()
        with patch("config.settings.FIELD_FILL_GAP_MS", 50):
            asyncio.run(filler.field_pacing(mock_el))
        elapsed = time.perf_counter() - start

        mock_el.wait_for.assert_awaited_once_with(state="visible", timeout=2000)
        self.assertGreaterEqual(elapsed, 0.04)


class TestBug2SubmissionVerificationGate(unittest.TestCase):
    """Bug 2: Submission verification gate prevents false success messages."""

    def test_verify_submission_url_confirmation(self):
        """Verify submission passes when page redirects to a confirmation URL."""
        mock_page = AsyncMock()
        mock_page.url = "https://jobs.lever.co/elevenlabs/confirmation"
        mock_page.content = AsyncMock(return_value="<html><body>Application Submitted</body></html>")

        verified, reason = asyncio.run(verify_submission(mock_page, timeout_sec=0.2))
        self.assertTrue(verified)
        self.assertIn("Confirmation URL pattern matched", reason)

    def test_verify_submission_text_confirmation(self):
        """Verify submission passes when confirmation text is present in DOM."""
        mock_page = AsyncMock()
        mock_page.url = "https://boards.greenhouse.io/stripe/jobs/12345"
        mock_page.content = AsyncMock(return_value="<div>Thank you for applying to Stripe! Your application has been received.</div>")

        verified, reason = asyncio.run(verify_submission(mock_page, timeout_sec=0.2))
        self.assertTrue(verified)
        self.assertIn("Confirmation text matched", reason)

    def test_verify_submission_validation_error_fails(self):
        """Verify submission fails if validation error messages are visible on page."""
        mock_page = AsyncMock()
        mock_page.url = "https://jobs.ashbyhq.com/openai/application"
        mock_page.content = AsyncMock(return_value="<div>There were errors with your submission. Please fill in this required field.</div>")

        mock_locator = AsyncMock()
        mock_locator.first.count = AsyncMock(return_value=0)
        mock_page.locator = MagicMock(return_value=mock_locator)

        verified, reason = asyncio.run(verify_submission(mock_page, timeout_sec=0.2))
        self.assertFalse(verified)
        self.assertIn("Validation error", reason)

    def test_verify_submission_unconfirmed_timeout_with_submit_btn(self):
        """Verify submission fails when submit button is still active and no confirmation exists."""
        mock_page = AsyncMock()
        mock_page.url = "https://jobs.ashbyhq.com/elevenlabs/apply"
        mock_page.content = AsyncMock(return_value="<html><body><form><input type='text'/></form></body></html>")

        def locator_side_effect(selector):
            mock_sub = MagicMock()
            if selector.startswith('button[type="submit"]'):
                mock_sub.first.count = AsyncMock(return_value=1)
                mock_sub.first.is_visible = AsyncMock(return_value=True)
            else:
                mock_sub.first.count = AsyncMock(return_value=0)
                mock_sub.first.is_visible = AsyncMock(return_value=False)
            return mock_sub

        mock_page.locator = MagicMock(side_effect=locator_side_effect)

        verified, reason = asyncio.run(verify_submission(mock_page, timeout_sec=0.2))
        self.assertFalse(verified)
        self.assertIn("Submit button still visible", reason)

    def test_stale_screenshot_cleanup(self):
        """Verify stale screenshots older than threshold are cleaned up."""
        filler = AutoFiller()
        with tempfile.TemporaryDirectory() as temp_dir:
            old_file = os.path.join(temp_dir, "old_test.png")
            new_file = os.path.join(temp_dir, "new_test.png")

            with open(old_file, "w") as f:
                f.write("old")
            with open(new_file, "w") as f:
                f.write("new")

            past_time = time.time() - (48 * 3600)
            os.utime(old_file, (past_time, past_time))

            filler.clean_stale_screenshots(max_age_hours=24, screenshot_dir=temp_dir)

            self.assertFalse(os.path.exists(old_file))
            self.assertTrue(os.path.exists(new_file))


class TestBug3ClearLeverContext(unittest.TestCase):
    """Bug 3: Clear cookies/localStorage/sessionStorage before Lever flows."""

    def test_clear_lever_context_invocations(self):
        """Verify clear_cookies and storage evaluate are invoked."""
        mock_context = AsyncMock()
        mock_context.clear_cookies = AsyncMock()

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock()
        mock_context.pages = [mock_page]

        asyncio.run(clear_lever_context(mock_context, mock_page))

        mock_context.clear_cookies.assert_awaited_once()
        mock_page.evaluate.assert_awaited_once()
        script = mock_page.evaluate.call_args[0][0]
        self.assertIn("localStorage.clear()", script)
        self.assertIn("sessionStorage.clear()", script)


class TestBug4HeadlessBrowserDefault(unittest.TestCase):
    """Bug 4: Browser runs in background (headless=True) unless DEBUG_HEADED=true."""

    def test_headless_default_is_true(self):
        """Verify headless is True by default."""
        with patch.dict(os.environ, {}, clear=True):
            settings = config.Settings()
            self.assertTrue(settings.HEADLESS)
            self.assertFalse(settings.DEBUG_HEADED)

    def test_debug_headed_override_flips_headless(self):
        """Verify setting DEBUG_HEADED=true enables headed mode (headless=False)."""
        with patch.dict(os.environ, {"DEBUG_HEADED": "true"}):
            settings = config.Settings()
            self.assertTrue(settings.DEBUG_HEADED)
            self.assertFalse(settings.HEADLESS)


class TestFeature1RequiredFieldCompleteness(unittest.TestCase):
    """Deliverable 1: Required-Field Completeness Gate & Knockout Compliance."""

    def test_compliance_config_keys_present(self):
        """Verify new compliance and knockout settings keys are loaded."""
        settings = config.Settings()
        self.assertTrue(hasattr(settings, "WORK_AUTH_STATUS"))
        self.assertTrue(hasattr(settings, "HYBRID_OFFICE_WILLINGNESS"))
        self.assertTrue(hasattr(settings, "PRIVACY_POLICY_CONSENT"))
        self.assertTrue(hasattr(settings, "RELOCATION_WILLINGNESS"))

    def test_deterministic_engine_benchling_work_auth_status(self):
        """Verify DeterministicEngine solves 'U.S. Work Authorization Status' select."""
        from form_filler import DeterministicEngine
        choices = [
            "U.S. Citizen / Permanent Resident",
            "F-1 (OPT/CPT) / Requires Visa Sponsorship",
            "H-1B Visa",
            "Not authorized"
        ]
        ans = DeterministicEngine.solve("U.S. Work Authorization Status", choices)
        self.assertIsNotNone(ans)
        self.assertIn("OPT", ans)

    def test_deterministic_engine_benchling_hybrid_office_question(self):
        """Verify DeterministicEngine answers Yes to in-office willingness (Rule 4 Addendum)."""
        from form_filler import DeterministicEngine
        ans = DeterministicEngine.solve("Are you able to work from our office five days a week?*", ["Yes", "No"])
        self.assertEqual(ans, "Yes")

    def test_deterministic_engine_benchling_privacy_policy_checkbox(self):
        """Verify DeterministicEngine solves Lever Privacy Policy consent."""
        from form_filler import DeterministicEngine
        ans = DeterministicEngine.solve("Candidate Privacy Notice & Consent to processing personal data", ["I acknowledge and agree", "No"])
        self.assertIn("acknowledge", ans.lower())

    def test_enumerate_required_fields_detects_unfilled(self):
        """Verify enumerate_required_fields detects unselected inputs."""
        from form_filler import enumerate_required_fields
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=[
            {"type": "text", "label": "Full Name", "value": "Siddhesh Sawant", "is_filled": True},
            {"type": "select", "label": "U.S. Work Authorization Status", "value": "", "is_filled": False, "choices": ["F-1", "H-1B"]},
            {"type": "checkbox", "label": "Privacy Policy", "value": "", "is_filled": False, "choices": []}
        ])
        fields = asyncio.run(enumerate_required_fields(mock_page))
        self.assertEqual(len(fields), 3)
        unfilled = [f for f in fields if not f["is_filled"]]
        self.assertEqual(len(unfilled), 2)

    def test_ensure_required_fields_filled_halts_on_unconfident_answer(self):
        """Verify ensure_required_fields_filled halts without guessing on unknown required questions."""
        from form_filler import ensure_required_fields_filled
        mock_page = AsyncMock()
        # Initial evaluation returns an unsolvable custom required question
        mock_page.evaluate = AsyncMock(return_value=[
            {"type": "text", "id": "custom_q1", "name": "custom_q1", "label": "What is your secret favorite color and why?", "value": "", "is_filled": False, "choices": []}
        ])
        success, missing = asyncio.run(ensure_required_fields_filled(mock_page, {}))
        self.assertFalse(success)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["label"], "What is your secret favorite color and why?")


class TestFeature2LLMPreSubmitVerificationGate(unittest.TestCase):
    """Deliverable 2: LLM Pre-Submit Verification Gate."""

    def test_pre_submit_audit_flags_empty_required_field(self):
        """Verify pre-submit audit flags when a required field is empty."""
        from app.services.llm_verifier import llm_pre_submit_audit
        mock_page = AsyncMock()
        mock_page.screenshot = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={
            "fields": [
                {"tag": "input", "type": "text", "label": "Full Name", "value": "Siddhesh Santosh Sawant", "required": True},
                {"tag": "input", "type": "email", "label": "Email", "value": "siddheshs202001@gmail.com", "required": True},
                {"tag": "input", "type": "tel", "label": "Phone", "value": "4452082299", "required": True},
                {"tag": "select", "type": "select", "label": "Work Authorization Status", "value": "", "required": True}
            ],
            "resume_attached": True
        })
        profile = {
            "personal_info": {"full_name": "Siddhesh Santosh Sawant", "email": "siddheshs202001@gmail.com", "phone": "4452082299"},
            "compliance_and_eeo": {"work_auth_status": "F-1 (OPT)"}
        }
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            passed, reason, issues = asyncio.run(llm_pre_submit_audit(mock_page, profile, tmp.name))
            self.assertFalse(passed)
            self.assertTrue(any("Work Authorization" in iss or "required" in iss.lower() for iss in issues))

    def test_pre_submit_audit_flags_placeholder_text(self):
        """Verify pre-submit audit flags when placeholder text is present."""
        from app.services.llm_verifier import llm_pre_submit_audit
        mock_page = AsyncMock()
        mock_page.screenshot = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={
            "fields": [
                {"tag": "input", "type": "text", "label": "Full Name", "value": "Siddhesh Santosh Sawant", "required": True},
                {"tag": "input", "type": "email", "label": "Email", "value": "siddheshs202001@gmail.com", "required": True},
                {"tag": "input", "type": "tel", "label": "Phone", "value": "4452082299", "required": True},
                {"tag": "select", "type": "select", "label": "Role Experience", "value": "Select...", "required": True}
            ],
            "resume_attached": True
        })
        profile = {
            "personal_info": {"full_name": "Siddhesh Santosh Sawant", "email": "siddheshs202001@gmail.com", "phone": "4452082299"}
        }
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            passed, reason, issues = asyncio.run(llm_pre_submit_audit(mock_page, profile, tmp.name))
            self.assertFalse(passed)
            self.assertTrue(any("placeholder" in iss.lower() for iss in issues))

    def test_pre_submit_audit_passes_complete_form(self):
        """Verify pre-submit audit passes when all required fields and profile match."""
        from app.services.llm_verifier import llm_pre_submit_audit
        mock_page = AsyncMock()
        mock_page.screenshot = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={
            "fields": [
                {"tag": "input", "type": "text", "label": "Full Name", "value": "Siddhesh Santosh Sawant", "required": True},
                {"tag": "input", "type": "email", "label": "Email", "value": "siddheshs202001@gmail.com", "required": True},
                {"tag": "input", "type": "tel", "label": "Phone", "value": "4452082299", "required": True},
                {"tag": "select", "type": "select", "label": "Work Authorization", "value": "Authorized / Require Sponsorship", "required": True},
                {"tag": "checkbox", "type": "checkbox", "label": "Privacy Policy", "value": "checked", "required": True}
            ],
            "resume_attached": True
        })
        profile = {
            "personal_info": {"full_name": "Siddhesh Santosh Sawant", "email": "siddheshs202001@gmail.com", "phone": "4452082299"},
            "compliance_and_eeo": {"work_auth_status": "Authorized / Require Sponsorship"}
        }
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            # Mock call_llm_json to return approved=True
            with patch("app.services.llm_verifier.call_llm_json", new=AsyncMock(return_value={"approved": True, "reason": "All good", "issues": []})):
                passed, reason, issues = asyncio.run(llm_pre_submit_audit(mock_page, profile, tmp.name))
                self.assertTrue(passed)
                self.assertEqual(len(issues), 0)


class TestFeature3LLMPostSubmitVerificationGate(unittest.TestCase):
    """Deliverable 3: LLM Post-Submit Verification Gate."""

    def test_post_submit_verify_confirmed_success(self):
        """Verify LLM post-submit verification passes on confirmed submission page."""
        from app.services.llm_verifier import llm_post_submit_verify
        mock_page = AsyncMock()
        mock_page.url = "https://jobs.lever.co/benchling/thanks"
        mock_page.content = AsyncMock(return_value="<html><body>Thank you for applying! Your application has been received.</body></html>")
        mock_page.screenshot = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value="Thank you for applying! Your application has been received.")

        mock_sub = MagicMock()
        mock_sub.first.count = AsyncMock(return_value=0)
        mock_sub.first.is_visible = AsyncMock(return_value=False)
        mock_page.locator = MagicMock(return_value=mock_sub)

        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            with patch("app.services.llm_verifier.call_llm_json", new=AsyncMock(return_value={"confirmed": True, "status": "confirmed_success", "reason": "Confirmed"})):
                verified, reason = asyncio.run(llm_post_submit_verify(mock_page, tmp.name, timeout_sec=0.2))
                self.assertTrue(verified)
                self.assertIn("Confirmed", reason)

    def test_post_submit_verify_halts_on_bot_block(self):
        """Verify post-submit verification strictly halts on anti-bot/CAPTCHA challenge."""
        from app.services.llm_verifier import llm_post_submit_verify
        mock_page = AsyncMock()
        mock_page.url = "https://jobs.lever.co/benchling/apply"
        mock_page.content = AsyncMock(return_value="<html><body>Access Denied: Cloudflare Turnstile security challenge active.</body></html>")
        mock_page.screenshot = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value="Access Denied")

        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            verified, reason = asyncio.run(llm_post_submit_verify(mock_page, tmp.name, timeout_sec=0.2))
            self.assertFalse(verified)
            self.assertIn("BOT/SECURITY BLOCK", reason)

    def test_post_submit_verify_fails_on_validation_errors(self):
        """Verify post-submit verification rejects when form validation errors remain."""
        from app.services.llm_verifier import llm_post_submit_verify
        mock_page = AsyncMock()
        mock_page.url = "https://jobs.lever.co/benchling/apply"
        mock_page.content = AsyncMock(return_value="<html><body>Please answer all required questions before submitting.</body></html>")
        mock_page.screenshot = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value="Please answer all required questions before submitting.")

        mock_sub = MagicMock()
        mock_sub.first.count = AsyncMock(return_value=1)
        mock_sub.first.is_visible = AsyncMock(return_value=True)
        mock_page.locator = MagicMock(return_value=mock_sub)

        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            with patch("app.services.llm_verifier.call_llm_json", new=AsyncMock(return_value={"confirmed": False, "status": "validation_error", "reason": "Missing required fields"})):
                verified, reason = asyncio.run(llm_post_submit_verify(mock_page, tmp.name, timeout_sec=0.2))
                self.assertFalse(verified)
                self.assertIn("Missing required fields", reason)


class TestOpportunityLocationChoiceResolution(unittest.TestCase):
    """Tests for opportunityLocationId / location dropdown resolution."""

    def test_pick_best_location_choice_waabi_lever(self):
        """Verify arbitrary non-DC hub choices return None to avoid unauthorized guessing (Rule 4)."""
        from form_filler import DeterministicEngine
        waabi_choices = ["Select...", "Phoenix, AZ", "San Francisco, CA", "Pittsburgh, PA"]
        chosen = DeterministicEngine.pick_best_location_choice(waabi_choices)
        self.assertIsNone(chosen)

    def test_pick_best_location_choice_remote_us(self):
        from form_filler import DeterministicEngine
        choices = ["Select...", "Toronto, ON", "Remote - US", "London, UK"]
        chosen = DeterministicEngine.pick_best_location_choice(choices)
        self.assertEqual(chosen, "Remote - US")

    def test_pick_best_location_choice_dc(self):
        from form_filler import DeterministicEngine
        choices = ["Select...", "Austin, TX", "Washington, DC"]
        chosen = DeterministicEngine.pick_best_location_choice(choices)
        self.assertEqual(chosen, "Washington, DC")

    def test_pick_best_location_choice_ignores_all_placeholders(self):
        """Verify non-matching locations return None rather than guessing the first item."""
        from form_filler import DeterministicEngine
        choices = ["Select...", "--", "Please select...", "Tokyo, Japan"]
        chosen = DeterministicEngine.pick_best_location_choice(choices)
        self.assertIsNone(chosen)

    def test_deterministic_engine_solve_which_location(self):
        """Verify selecting from arbitrary hub locations triggers HUB_NOT_NEAR_RESIDENCE (Rule 4)."""
        from form_filler import DeterministicEngine
        from app.services.form_guards import FormHaltException
        waabi_choices = ["Select...", "Phoenix, AZ", "San Francisco, CA", "Pittsburgh, PA"]
        with self.assertRaises(FormHaltException) as ctx:
            DeterministicEngine.solve("Which location are you applying for?", waabi_choices)
        self.assertEqual(ctx.exception.code, "HUB_NOT_NEAR_RESIDENCE")

    def test_deterministic_engine_solve_opportunity_location(self):
        """Verify opportunity location without DC/Remote triggers HUB_NOT_NEAR_RESIDENCE (Rule 4)."""
        from form_filler import DeterministicEngine
        from app.services.form_guards import FormHaltException
        waabi_choices = ["Select...", "Phoenix, AZ", "Pittsburgh, PA"]
        with self.assertRaises(FormHaltException) as ctx:
            DeterministicEngine.solve("opportunityLocationId", waabi_choices)
        self.assertEqual(ctx.exception.code, "HUB_NOT_NEAR_RESIDENCE")

    def test_common_field_resolver_opportunity_location(self):
        """Verify common field resolver returns None when no DC/Remote option exists."""
        from app.services.common_fields import common_field_resolver
        waabi_choices = ["Select...", "Phoenix, AZ", "San Francisco, CA", "Pittsburgh, PA"]
        ans = common_field_resolver.solve("Which location are you applying for?", waabi_choices)
        self.assertIsNone(ans)

    def test_universal_registry_opportunity_location(self):
        """Verify universal registry returns None when no DC/Remote option exists."""
        from app.services.universal_registry import universal_solver
        waabi_choices = ["Select...", "Phoenix, AZ", "San Francisco, CA", "Pittsburgh, PA"]
        ans, prov, stage = universal_solver.solve("Which location are you applying for?", waabi_choices)
        self.assertIsNone(ans)


class TestCooldownSubmissionScoped(unittest.TestCase):
    """Verify cooldown is strictly triggered by status='submitted' only."""

    def setUp(self):
        from app.services.company_cap import company_cap_tracker
        self.tracker = company_cap_tracker
        self.tracker.reset_cap("TestCooldownCo")

    def tearDown(self):
        self.tracker.reset_cap("TestCooldownCo")

    def test_failed_attempt_does_not_trigger_cooldown(self):
        # Record an 'attempted' or 'submit_failed' submission
        self.tracker.record_submission("TestCooldownCo", "SWE", "https://jobs.lever.co/testcooldownco/123", status="submit_failed")
        is_capped, reason, count = self.tracker.is_company_capped("TestCooldownCo", "https://jobs.lever.co/testcooldownco/123")
        self.assertFalse(is_capped, f"Failed attempt must NOT trigger cooldown, but got: {reason}")

    def test_submitted_triggers_cooldown(self):
        self.tracker.record_submission("TestCooldownCo", "SWE", "https://jobs.lever.co/testcooldownco/123", status="submitted")
        is_capped, reason, count = self.tracker.is_company_capped("TestCooldownCo", "https://jobs.lever.co/testcooldownco/123")
        self.assertTrue(is_capped, "Submitted application MUST trigger cooldown")
        self.assertIn("cooldown active", reason.lower())


if __name__ == "__main__":
    unittest.main()
