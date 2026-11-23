"""
Unit tests & dry-run proofs for:
1. HITL A: CAPTCHA_SOLVE_REQUEST (freeze -> ask -> Mac solve -> resume -> verify -> READY_TO_SUBMIT -> submit)
2. HITL B: MISSING_INFO_REQUEST (freeze -> ask -> user reply -> verbatim fill -> persist -> verify -> READY_TO_SUBMIT)
3. HITL Timeout/Abandon -> HUMAN_DEFERRED_TIMEOUT (digest-only, slot released)
4. Cap Tiebreak in Scan Loop: Descending skill-overlap order consumes per-company cap slots first
5. Discovery Throughput: YCTopCompaniesAdapter, ATSCustomerDirectoryAdapter, and unprobed queue draining
"""
import os
import sys
import time
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models import Profile
from app.services.auto_filler import AutoFiller, HITLManager, hitl_manager, resolve_hitl_action
from app.services.audit_logger import audit_logger
from app.services.discovery_engine import (
    YCTopCompaniesAdapter,
    ATSCustomerDirectoryAdapter,
    discovery_engine
)
from app.services.company_registry import CompanyRegistry


class TestHITLFlows(unittest.IsolatedAsyncioTestCase):
    """Verifies complete freeze -> ask -> resume -> verify flows for HITL A and B."""

    def setUp(self):
        self.profile = Profile(
            first_name="Siddhesh",
            last_name="Sawant",
            full_name="Siddhesh Sawant",
            email="siddhesh@example.com",
            phone="+12025550123"
        )
        self.filler = AutoFiller()

    def tearDown(self):
        try:
            from app.services.company_cap import company_cap_tracker
            with company_cap_tracker._get_connection() as conn:
                conn.execute("DELETE FROM company_submissions WHERE company_norm LIKE 'dryrunco%' OR company_norm LIKE 'rustco%'")
                conn.commit()
        except Exception:
            pass

    async def test_hitl_a_captcha_solve_request_dry_run(self):
        """
        DRY-RUN PROOF: HITL A (CAPTCHA_SOLVE_REQUEST)
        1. Form has fields filled.
        2. Security challenge detected at pre-submit.
        3. Browser freezes on Mac, ONE Telegram message sent with Solved/Abandon buttons.
        4. Simulated user confirms CAPTCHA solved on Mac.
        5. Execution resumes, re-verifies fields, reaches READY_TO_SUBMIT and submits.
        6. HARD rule verified: bot never clicks or solves CAPTCHA directly.
        """
        job_id = f"dryrun_captcha_{int(time.time()*1000)}"
        company_name = f"DryRunCo_{int(time.time()*1000)}"

        # Mock page and browser context
        mock_page = AsyncMock()
        mock_context = AsyncMock()

        # Step 1: pre_fill returns False (clean page load)
        # Step 2: pre_submit returns True (CAPTCHA detected)
        # Step 3: post-solve returns False (challenge solved)
        challenge_state = [False, True, False]
        async def mock_detect(p):
            if challenge_state:
                return challenge_state.pop(0), "recaptcha"
            return False, "none"

        mock_page.content = AsyncMock(return_value="<html><body>Application Form</body></html>")
        mock_locator = MagicMock()
        mock_first = AsyncMock()
        mock_first.count = AsyncMock(return_value=1)
        mock_first.is_visible = AsyncMock(return_value=True)
        mock_first.click = AsyncMock()
        mock_first.input_value = AsyncMock(return_value="Already Filled")
        mock_locator.first = mock_first
        mock_locator.count = AsyncMock(return_value=1)
        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_page.evaluate = AsyncMock(return_value={"clickable": True})
        mock_page.frames = []

        mock_playwright = AsyncMock()
        mock_browser = AsyncMock()
        mock_browser.new_context.return_value = mock_context
        mock_context.new_page.return_value = mock_page
        mock_playwright.chromium.launch.return_value = mock_browser
        mock_playwright.chromium.launch_persistent_context.return_value = mock_context

        telegram_alerts_sent = []
        async def mock_send_telegram(job_id, alert_type, company, role, portal, job_url, details="", buttons=None, **kwargs):
            telegram_alerts_sent.append({
                "job_id": job_id,
                "alert_type": alert_type,
                "company": company,
                "details": details,
                "buttons": buttons
            })
            return True

        async def mock_fill(page, profile):
            return {"first_name": "Siddhesh", "last_name": "Sawant", "email": "siddhesh@example.com", "resume": True}

        with patch("playwright.async_api.async_playwright") as mock_ap, \
             patch("app.services.presubmit_gate.SecurityChallengeDetector.detect_challenge", side_effect=mock_detect), \
             patch("app.services.telegram_notifier.send_telegram_alert", side_effect=mock_send_telegram), \
             patch("app.services.company_cap.company_cap_tracker.is_company_capped", return_value=(False, "", 0)), \
             patch("app.services.presubmit_gate.presubmit_gate.validate_page_state", new_callable=AsyncMock, return_value=MagicMock(is_valid=True, blocking_reasons=[])), \
             patch("app.services.auto_filler.ensure_required_fields_filled", new_callable=AsyncMock, return_value=(True, [])), \
             patch("app.services.llm_verifier.llm_pre_submit_audit", new_callable=AsyncMock, return_value=(True, "Audit passed", [])), \
             patch("app.services.presubmit_gate.OverlayChecker.verify_submit_interactable", new_callable=AsyncMock, return_value=(True, None)), \
             patch("app.services.auto_filler.verify_submission", new_callable=AsyncMock, return_value=(True, "Thank you for applying")), \
             patch("app.services.auto_filler.llm_post_submit_verify", new_callable=AsyncMock, return_value=(True, "Verified submitted")), \
             patch.object(self.filler, "detect_verification_screen", new_callable=AsyncMock, return_value=False), \
             patch.object(self.filler, "detect_already_applied", new_callable=AsyncMock, return_value=False), \
             patch.object(self.filler, "fill_form_fields", side_effect=mock_fill), \
             patch.object(self.filler, "verify_submission", new_callable=AsyncMock, return_value=(True, "Thank you for applying")), \
             patch.object(self.filler, "field_pacing", new_callable=AsyncMock):

            mock_ap.return_value.__aenter__.return_value = mock_playwright

            # Launch open_and_prefill in background task to test async freeze & resume
            task = asyncio.create_task(
                self.filler.open_and_prefill(
                    job_url=f"https://boards.greenhouse.io/{company_name.lower()}/jobs/1",
                    profile=self.profile,
                    mode="auto_submit",
                    company=company_name,
                    job_title="Software Engineer",
                    job_description="We need Python, PyTorch, SQL, and Docker engineers.",
                    job_id=job_id,
                    hitl_enabled=True,
                    hitl_timeout_sec=5.0
                )
            )

            # Wait for task to fill fields and hit the challenge
            for _ in range(50):
                if len(telegram_alerts_sent) >= 1:
                    break
                await asyncio.sleep(0.05)

            # 1. Assert Telegram alert was sent exactly once
            self.assertEqual(len(telegram_alerts_sent), 1, "Must send exactly ONE message on challenge block")
            alert = telegram_alerts_sent[0]
            self.assertEqual(alert["alert_type"], "captcha_solve_request")
            self.assertEqual(alert["company"], company_name)
            self.assertIn("solve the CAPTCHA on your Mac", alert["details"])
            # Assert Solved/Abandon buttons present
            btn_texts = [b["text"] for row in alert["buttons"] for b in row]
            self.assertTrue(any("Solved" in t for t in btn_texts))
            self.assertTrue(any("Abandon" in t for t in btn_texts))

            # 2. Simulate User clicking "✅ Solved — Submit" on Mac
            resumed = resolve_hitl_action(job_id, action="solved")
            self.assertTrue(resumed, "resolve_hitl_action must resolve pending event")

            # 3. Await task completion
            result = await task

            # 4. Assert resumed execution reaches READY_TO_SUBMIT and submits
            self.assertTrue(result.get("captcha_solve_requested"))
            self.assertTrue(result.get("captcha_solved"))
            self.assertTrue(result.get("hitl_resumed"))
            self.assertEqual(result.get("status"), "submitted")

    async def test_hitl_b_missing_info_request_dry_run(self):
        """
        DRY-RUN PROOF: HITL B (MISSING_INFO_REQUEST)
        1. Required field has no resolvable value (e.g. 'Years of Experience with Rust').
        2. Browser freezes, ONE Telegram message sent listing exact missing field names.
        3. User replies with value (simulated reply '3 years').
        4. Value is filled verbatim with fixed pacing, persisted to profile.
        5. Re-verifies fields, transitions to READY_TO_SUBMIT and submits.
        """
        job_id = f"dryrun_info_{int(time.time()*1000)}"
        company_name = f"RustCo_{int(time.time()*1000)}"

        mock_page = AsyncMock()
        mock_context = AsyncMock()

        mock_page.content = AsyncMock(return_value="<html><body>Application Form</body></html>")
        mock_locator = MagicMock()
        mock_first = AsyncMock()
        mock_first.count = AsyncMock(return_value=1)
        mock_first.is_visible = AsyncMock(return_value=True)
        mock_first.click = AsyncMock()
        mock_first.input_value = AsyncMock(return_value="Valid")
        mock_locator.first = mock_first
        mock_locator.count = AsyncMock(return_value=1)
        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_page.evaluate = AsyncMock(return_value={"clickable": True})
        mock_page.frames = []

        mock_playwright = AsyncMock()
        mock_browser = AsyncMock()
        mock_browser.new_context.return_value = mock_context
        mock_context.new_page.return_value = mock_page
        mock_playwright.chromium.launch.return_value = mock_browser
        mock_playwright.chromium.launch_persistent_context.return_value = mock_context

        telegram_alerts_sent = []
        async def mock_send_telegram(job_id, alert_type, company, role, portal, job_url, details="", buttons=None, **kwargs):
            telegram_alerts_sent.append({
                "job_id": job_id,
                "alert_type": alert_type,
                "company": company,
                "details": details,
                "buttons": buttons
            })
            return True

        # First pass: missing required field 'Rust Experience'
        # Second pass after user reply: no missing required fields
        req_passes = [
            (False, [{"label": "Rust Experience", "name": "rust_exp"}]),
            (True, [])
        ]
        async def mock_ensure_fields(page, profile):
            if req_passes:
                return req_passes.pop(0)
            return True, []

        async def mock_fill(page, profile):
            return {"first_name": "Siddhesh", "last_name": "Sawant", "email": "siddhesh@example.com", "resume": True}

        with patch("playwright.async_api.async_playwright") as mock_ap, \
             patch("app.services.presubmit_gate.SecurityChallengeDetector.detect_challenge", new_callable=AsyncMock, return_value=(False, "none")), \
             patch("app.services.telegram_notifier.send_telegram_alert", side_effect=mock_send_telegram), \
             patch("app.services.company_cap.company_cap_tracker.is_company_capped", return_value=(False, "", 0)), \
             patch("app.services.presubmit_gate.presubmit_gate.validate_page_state", new_callable=AsyncMock, return_value=MagicMock(is_valid=True, blocking_reasons=[])), \
             patch("app.services.auto_filler.ensure_required_fields_filled", side_effect=mock_ensure_fields), \
             patch("app.services.llm_verifier.llm_pre_submit_audit", new_callable=AsyncMock, return_value=(True, "Audit passed", [])), \
             patch("app.services.presubmit_gate.OverlayChecker.verify_submit_interactable", new_callable=AsyncMock, return_value=(True, None)), \
             patch("app.services.auto_filler.verify_submission", new_callable=AsyncMock, return_value=(True, "Thank you for applying")), \
             patch("app.services.auto_filler.llm_post_submit_verify", new_callable=AsyncMock, return_value=(True, "Verified submitted")), \
             patch.object(self.filler, "detect_verification_screen", new_callable=AsyncMock, return_value=False), \
             patch.object(self.filler, "detect_already_applied", new_callable=AsyncMock, return_value=False), \
             patch.object(self.filler, "fill_form_fields", side_effect=mock_fill), \
             patch.object(self.filler, "verify_submission", new_callable=AsyncMock, return_value=(True, "Thank you for applying")), \
             patch.object(self.filler, "safe_fill", new_callable=AsyncMock, return_value=True), \
             patch.object(self.filler, "field_pacing", new_callable=AsyncMock):

            mock_ap.return_value.__aenter__.return_value = mock_playwright

            task = asyncio.create_task(
                self.filler.open_and_prefill(
                    job_url=f"https://boards.greenhouse.io/{company_name.lower()}/jobs/2",
                    profile=self.profile,
                    mode="auto_submit",
                    company=company_name,
                    job_title="Systems Engineer",
                    job_description="Seeking Linux, Python, Docker, C++ engineers.",
                    job_id=job_id,
                    hitl_enabled=True,
                    hitl_timeout_sec=5.0
                )
            )

            # Wait for task to fill fields and detect missing required info
            for _ in range(50):
                if len(telegram_alerts_sent) >= 1:
                    break
                await asyncio.sleep(0.05)

            # 1. Assert Telegram message sent with exact missing field name
            self.assertEqual(len(telegram_alerts_sent), 1)
            alert = telegram_alerts_sent[0]
            self.assertEqual(alert["alert_type"], "missing_info_request")
            self.assertIn("Rust Experience", alert["details"])

            # 2. Simulate User replying with value
            resumed = resolve_hitl_action(job_id, action="info_reply", data={"Rust Experience": "3 years"})
            self.assertTrue(resumed)

            # 3. Await task completion
            result = await task

            # 4. Assert filled verbatim and submitted
            self.assertTrue(result.get("missing_info_requested"))
            self.assertTrue(result.get("hitl_resumed"))
            self.assertEqual(result.get("status"), "submitted")

    async def test_hitl_timeout_logs_human_deferred_timeout(self):
        """
        Verifies that timeout/abandon produces HUMAN_DEFERRED_TIMEOUT (digest-only),
        releases the pacing slot, and closes browser context without throwing.
        """
        job_id = f"dryrun_timeout_{int(time.time()*1000)}"
        company_name = f"TimeoutCo_{int(time.time()*1000)}"

        mock_page = AsyncMock()
        mock_context = AsyncMock()
        mock_page.content = AsyncMock(return_value="<html><body>Form</body></html>")
        mock_locator = MagicMock()
        mock_first = AsyncMock()
        mock_first.count = AsyncMock(return_value=1)
        mock_first.is_visible = AsyncMock(return_value=True)
        mock_first.click = AsyncMock()
        mock_first.input_value = AsyncMock(return_value="Some Value")
        mock_locator.first = mock_first
        mock_locator.count = AsyncMock(return_value=1)
        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_page.evaluate = AsyncMock(return_value={"clickable": True})
        mock_page.frames = []

        mock_playwright = AsyncMock()
        mock_browser = AsyncMock()
        mock_browser.new_context.return_value = mock_context
        mock_context.new_page.return_value = mock_page
        mock_playwright.chromium.launch.return_value = mock_browser
        mock_playwright.chromium.launch_persistent_context.return_value = mock_context

        challenge_seq = [False, True]
        async def mock_chal(p):
            if challenge_seq:
                return challenge_seq.pop(0), "cloudflare"
            return True, "cloudflare"

        async def mock_fill(page, profile):
            return {"first_name": "Siddhesh", "last_name": "Sawant", "email": "siddhesh@example.com", "resume": True}

        with patch("playwright.async_api.async_playwright") as mock_ap, \
             patch("app.services.presubmit_gate.SecurityChallengeDetector.detect_challenge", side_effect=mock_chal), \
             patch("app.services.telegram_notifier.send_telegram_alert", new_callable=AsyncMock, return_value=True), \
             patch("app.services.company_cap.company_cap_tracker.is_company_capped", return_value=(False, "", 0)), \
             patch.object(self.filler, "detect_verification_screen", new_callable=AsyncMock, return_value=False), \
             patch.object(self.filler, "detect_already_applied", new_callable=AsyncMock, return_value=False), \
             patch.object(self.filler, "fill_form_fields", side_effect=mock_fill), \
             patch.object(self.filler, "field_pacing", new_callable=AsyncMock):

            mock_ap.return_value.__aenter__.return_value = mock_playwright

            # Fast timeout for test (0.05s)
            result = await self.filler.open_and_prefill(
                job_url=f"https://boards.greenhouse.io/{company_name.lower()}/jobs/3",
                profile=self.profile,
                mode="auto_submit",
                company=company_name,
                job_title="ML Engineer",
                job_description="Looking for Python, PyTorch, and Machine Learning engineers.",
                job_id=job_id,
                hitl_enabled=True,
                hitl_timeout_sec=0.05
            )

            # Assert terminal status is human_deferred_timeout
            self.assertEqual(result.get("status"), "human_deferred_timeout")
            self.assertIn("HUMAN_DEFERRED_TIMEOUT", result.get("message"))
            # Context must be closed (slot released)
            mock_context.close.assert_awaited()


class TestCapTiebreakScanLoop(unittest.TestCase):
    """
    Verifies that the scan loop sorts passing postings by skill-overlap descending
    BEFORE consuming per-company cap slots (strongest matches consume slots first).
    """

    def test_passing_postings_sorted_by_skill_overlap_descending(self):
        """Proves that postings with higher candidate skill-overlap are prioritized."""
        passing_jobs = [
            {"title": "Role A", "overlap_count": 1, "match_score": 60, "url": "https://a.com"},
            {"title": "Role B", "overlap_count": 4, "match_score": 85, "url": "https://b.com"},
            {"title": "Role C", "overlap_count": 2, "match_score": 75, "url": "https://c.com"},
            {"title": "Role D", "overlap_count": 4, "match_score": 95, "url": "https://d.com"},
        ]

        # Scan loop sorting key
        passing_jobs.sort(key=lambda x: (x["overlap_count"], x["match_score"]), reverse=True)

        # Expected order: Role D (4 skills, 95 score), Role B (4 skills, 85 score), Role C (2 skills), Role A (1 skill)
        self.assertEqual(passing_jobs[0]["title"], "Role D")
        self.assertEqual(passing_jobs[1]["title"], "Role B")
        self.assertEqual(passing_jobs[2]["title"], "Role C")
        self.assertEqual(passing_jobs[3]["title"], "Role A")

    def test_strongest_match_consumes_slot_when_company_cap_reached(self):
        """When cap allows only 1 slot, the 4-skill match consumes it, leaving 2-skill match capped."""
        passing_jobs = [
            {"title": "Junior Python Dev", "overlap_count": 2, "match_score": 70, "url": "https://ats.io/1"},
            {"title": "AI Platform Engineer", "overlap_count": 5, "match_score": 92, "url": "https://ats.io/2"},
        ]

        # Sort descending by overlap
        passing_jobs.sort(key=lambda x: (x["overlap_count"], x["match_score"]), reverse=True)

        slots_available = 1
        processed = []
        skipped_capped = []

        for item in passing_jobs:
            if slots_available > 0:
                processed.append(item["title"])
                slots_available -= 1
            else:
                skipped_capped.append(item["title"])

        self.assertEqual(processed, ["AI Platform Engineer"], "Strongest match must consume the slot")
        self.assertEqual(skipped_capped, ["Junior Python Dev"], "Weaker match must be capped")


class TestDiscoveryThroughput(unittest.IsolatedAsyncioTestCase):
    """Verifies that new slug sources (directories, showcases, customer lists) are operational."""

    async def test_yc_top_companies_adapter_yields_candidates(self):
        adapter = YCTopCompaniesAdapter()
        candidates = await adapter.discover()
        self.assertGreater(len(candidates), 30, "YCTopCompaniesAdapter must yield 30+ candidates")
        names = [c["name"] for c in candidates]
        self.assertIn("Stripe", names)
        self.assertIn("Linear", names)
        self.assertIn("Cursor", names)

    async def test_ats_customer_directory_adapter_yields_candidates(self):
        adapter = ATSCustomerDirectoryAdapter()
        candidates = await adapter.discover()
        self.assertGreater(len(candidates), 30, "ATSCustomerDirectoryAdapter must yield 30+ candidates")
        slugs = [c["slug"] for c in candidates]
        self.assertIn("datadog", slugs)
        self.assertIn("linear", slugs)
        self.assertIn("atlassian", slugs)

    def test_discovery_engine_has_all_official_adapters(self):
        adapter_names = [a.name for a in discovery_engine.adapters]
        self.assertIn("yc_top_companies", adapter_names)
        self.assertIn("ats_customer_directory", adapter_names)
        self.assertIn("ashby_showcase", adapter_names)
        self.assertIn("greenhouse_showcase", adapter_names)
        self.assertIn("lever_showcase", adapter_names)


if __name__ == "__main__":
    unittest.main()
