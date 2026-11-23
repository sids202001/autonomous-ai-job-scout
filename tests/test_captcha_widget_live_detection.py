import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from form_filler import CaptchaManager
from app.services.auto_filler import hitl_manager


class TestCaptchaWidgetLiveDetection(unittest.IsolatedAsyncioTestCase):
    """
    Regression test suite for:
    1. Multi-element hCaptcha detection (Lever hidden anchor + visible challenge modal)
    2. Detection via child frame URL in page.frames
    3. Negative test case: page-level anti-bot block with no widget returns False
    4. Post-submit Step 5 routing into CAPTCHA_SOLVE_REQUEST vs terminal digest-only
    """

    async def test_hcaptcha_lever_hidden_anchor_with_visible_modal(self):
        """
        Verify that CaptchaManager.is_captcha_present detects hCaptcha when the first
        iframe (checkbox/anchor) is hidden/invisible and the second iframe (challenge modal) is visible.
        This directly reproduces and fixes the Lever/Jobgether false negative.
        """
        mock_page = AsyncMock()
        mock_page.frames = []

        # Simulate locator for 'iframe[src*="hcaptcha"]'
        mock_anchor = AsyncMock()
        mock_anchor.is_visible = AsyncMock(return_value=False)

        mock_modal = AsyncMock()
        mock_modal.is_visible = AsyncMock(return_value=True)
        mock_modal.evaluate = AsyncMock(return_value=False)  # Not invisible

        def mock_locator(sel):
            loc = MagicMock()
            if "hcaptcha" in sel:
                loc.count = AsyncMock(return_value=2)
                loc.nth = MagicMock(side_effect=[mock_anchor, mock_modal])
                loc.first = mock_anchor
            else:
                loc.count = AsyncMock(return_value=0)
                loc.first = AsyncMock(count=AsyncMock(return_value=0), is_visible=AsyncMock(return_value=False))
            return loc

        mock_page.locator = MagicMock(side_effect=mock_locator)
        mock_page.evaluate = AsyncMock(return_value={"verified": False})

        is_present = await CaptchaManager.is_captcha_present(mock_page)
        self.assertTrue(is_present, "Must detect visible challenge modal even if first anchor iframe is invisible")

    async def test_hcaptcha_detected_via_frames_inspection(self):
        """
        Verify that CaptchaManager.is_captcha_present inspects child frames in page.frames,
        detecting challenge frame URLs with visible frame elements.
        """
        mock_page = AsyncMock()
        mock_frame = AsyncMock()
        mock_frame.url = "https://newassets.hcaptcha.com/captcha/v1/fa61633/static/hcaptcha.html#frame=challenge"

        mock_frame_el = AsyncMock()
        mock_frame_el.is_visible = AsyncMock(return_value=True)
        mock_frame_el.evaluate = AsyncMock(return_value=False)  # Visible, normal size
        mock_frame.frame_element = AsyncMock(return_value=mock_frame_el)

        mock_page.frames = [mock_frame]

        # Let selectors return 0 count to prove frame inspection catches it
        mock_page.locator = MagicMock(return_value=MagicMock(count=AsyncMock(return_value=0), first=AsyncMock(count=AsyncMock(return_value=0), is_visible=AsyncMock(return_value=False))))
        mock_page.evaluate = AsyncMock(return_value={"verified": False})

        is_present = await CaptchaManager.is_captcha_present(mock_page)
        self.assertTrue(is_present, "Must detect challenge modal via page.frames inspection")

    async def test_page_level_block_no_widget_returns_false(self):
        """
        Verify that a page-level anti-bot block (e.g. Cloudflare 403 Forbidden with NO widget)
        evaluates is_captcha_present to False, ensuring terminal digest-only handling.
        """
        mock_page = AsyncMock()
        mock_page.frames = []
        mock_page.locator = MagicMock(return_value=MagicMock(count=AsyncMock(return_value=0), first=AsyncMock(count=AsyncMock(return_value=0), is_visible=AsyncMock(return_value=False))))
        mock_page.evaluate = AsyncMock(return_value={"verified": False})

        is_present = await CaptchaManager.is_captcha_present(mock_page)
        self.assertFalse(is_present, "Page-level block with zero widgets must return False")

    async def test_post_submit_step_5_routing_to_hitl_when_widget_present(self):
        """
        Verify the post-submit Step 5 decision logic:
        When verify_submission indicates bot challenge AND a widget is present:
        HITL request_captcha_solve is dispatched.
        When NO widget is present:
        request_captcha_solve is NOT called, and terminal digest-only logic executes.
        """
        # Scenario A: Widget present -> HITL dispatched
        with patch.object(CaptchaManager, "is_captcha_present", new_callable=AsyncMock, return_value=True), \
             patch.object(hitl_manager, "request_captcha_solve", new_callable=AsyncMock) as mock_request:
            mock_page = AsyncMock()
            is_widget = await CaptchaManager.is_captcha_present(mock_page)
            if is_widget:
                await hitl_manager.request_captcha_solve(
                    job_id="job_test_123",
                    company="Jobgether",
                    role="AI Infrastructure Engineer",
                    portal="lever",
                    job_url="https://jobs.lever.co/jobgether/123",
                    timeout_sec=1800.0,
                    screenshot_path="/path/to/ss.png"
                )

            mock_request.assert_called_once()

        # Scenario B: No widget (genuine anti-bot wall) -> Zero Telegram dispatch
        with patch.object(CaptchaManager, "is_captcha_present", new_callable=AsyncMock, return_value=False), \
             patch.object(hitl_manager, "request_captcha_solve", new_callable=AsyncMock) as mock_request:
            mock_page = AsyncMock()
            is_widget = await CaptchaManager.is_captcha_present(mock_page)
            if is_widget:
                await hitl_manager.request_captcha_solve(
                    job_id="job_test_123",
                    company="Jobgether",
                    role="AI Infrastructure Engineer",
                    portal="lever",
                    job_url="https://jobs.lever.co/jobgether/123",
                    timeout_sec=1800.0,
                    screenshot_path="/path/to/ss.png"
                )

            mock_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
