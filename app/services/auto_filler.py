"""
AutoApply - Advanced Auto-Fill & Auto-Apply Engine
Supports 3 modes: review, auto_submit, batch
Features: anti-detection, CAPTCHA detection, screenshots, application logging,
multi-step form handling, aria-label field matching, robust launch fallback.

SAFETY:
- 'review' mode (default): fills form, takes screenshot, keeps browser open
- 'auto_submit': fills, screenshots, submits, logs result
- 'batch': processes multiple jobs sequentially in auto_submit mode
"""
import logging
import asyncio
import os
import json
import random
import time
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
import sys
import re
try:
    from app.models import Profile
except (ImportError, ModuleNotFoundError, Exception):
    Profile = Any

try:
    from app.services.llm_verifier import llm_pre_submit_audit, llm_post_submit_verify
except ImportError:
    try:
        from llm_verifier import llm_pre_submit_audit, llm_post_submit_verify
    except ImportError:
        llm_pre_submit_audit = None
        llm_post_submit_verify = None

try:
    from form_filler import ensure_required_fields_filled, enumerate_required_fields
except ImportError:
    ensure_required_fields_filled = None
    enumerate_required_fields = None

from app.services.inactivity_watchdog import InactivityWatchdog

logger = logging.getLogger(__name__)


async def clear_lever_context(context, page=None):
    """
    Clear cookies, localStorage, and sessionStorage before Lever applications.
    Does NOT touch SQLite DB or applied-jobs history.
    """
    try:
        if context and hasattr(context, "clear_cookies"):
            await context.clear_cookies()
            logger.info("Cleared browser cookies for Lever context.")

        target_pages = [page] if page else (getattr(context, "pages", []) if context else [])
        for p in target_pages:
            try:
                await p.evaluate("""() => {
                    try { window.localStorage.clear(); } catch(e) {}
                    try { window.sessionStorage.clear(); } catch(e) {}
                }""")
                logger.info("Cleared localStorage and sessionStorage for Lever context.")
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Failed to fully clear Lever context: {e}")


CONFIRMATION_TEXT_PATTERNS = [
    "thank you for applying",
    "thanks for applying",
    "thank you for your application",
    "application submitted",
    "application was submitted",
    "application has been submitted",
    "application received",
    "application has been received",
    "your application was sent",
    "your application has been sent",
    "we have received your application",
    "we've received your application",
    "we received your application",
    "thank you for your interest",
    "track your application",
    "we wanted to let you know we received",
    "submission received",
    "submission successful",
    "successfully applied",
    "successfully submitted",
    "profile submitted",
]


async def verify_submission(page, timeout_sec: float = 10.0) -> tuple[bool, str]:
    """
    Submission Verification Gate:
    Verifies that the job application was genuinely accepted by the ATS.
    Never assumes clicking submit succeeded without affirmative confirmation.

    Checks:
    1. Confirmation URL patterns (/thank, /confirmation, /submitted, /success, etc.)
    2. Confirmation heading/text in page content
    3. Absence of validation errors, alerts, or missing required field notices
    4. Form dismounting/disappearance vs form still present with errors

    Returns:
        (is_verified: bool, reason: str)
    """
    CONFIRMATION_URL_PATTERNS = [
        "confirmation", "thank", "thanks", "submitted", "success",
        "applied", "complete", "received", "application_submitted", "application-received"
    ]

    CONFIRMATION_TEXT_PATTERNS = [
        "thank you for applying",
        "thanks for applying",
        "thank you for your application",
        "application submitted",
        "application was submitted",
        "application has been submitted",
        "application received",
        "application has been received",
        "your application was sent",
        "your application has been sent",
        "we have received your application",
        "we've received your application",
        "we received your application",
        "thank you for your interest",
        "track your application",
        "we wanted to let you know we received",
        "submission received",
        "submission successful",
        "successfully applied",
        "successfully submitted",
        "profile submitted",
    ]

    ERROR_TEXT_PATTERNS = [
        "please fill in this required field",
        "this field is required",
        "please enter a valid",
        "please correct the errors",
        "there were errors with your submission",
        "there was a problem submitting",
        "submission failed",
        "an error occurred while submitting",
        "please upload a resume",
        "please answer this question",
    ]

    start_time = time.time()
    while time.time() - start_time < timeout_sec:
        try:
            # Check for security challenge immediately in the polling loop
            try:
                from app.services.presubmit_gate import SecurityChallengeDetector
                is_chal, chal_t = await SecurityChallengeDetector.detect_challenge(page)
                if is_chal:
                    return False, f"Security challenge detected: {chal_t}"
            except Exception:
                pass

            current_url = page.url.lower()
            for pattern in CONFIRMATION_URL_PATTERNS:
                if pattern in current_url:
                    return True, f"Confirmation URL pattern matched: {pattern}"

            content = (await page.content()).lower()
            for text_pat in CONFIRMATION_TEXT_PATTERNS:
                if text_pat in content:
                    return True, f"Confirmation text matched: '{text_pat}'"

            confirmation_selectors = [
                '[data-qa="confirmation-message"]',
                '.application-confirmation',
                '.submission-success',
                '.success-message',
                '[data-qa="application-submitted"]',
                'h1:has-text("Thank you")',
                'h2:has-text("Thank you")',
                'h1:has-text("Application Received")',
                'h2:has-text("Application Received")',
            ]
            for sel in confirmation_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        return True, f"Confirmation element visible: {sel}"
                except Exception:
                    pass

            error_selectors = [
                '[role="alert"]:visible',
                '.error-message:visible',
                '.field-error:visible',
                '.inline-error:visible',
                '.has-error:visible',
                '[aria-invalid="true"]:visible',
                '.form-error:visible',
            ]
            for sel in error_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        err_text = (await el.text_content() or "").strip()
                        if err_text:
                            return False, f"Validation error visible: {err_text[:120]}"
                except Exception:
                    pass

            for err_pat in ERROR_TEXT_PATTERNS:
                if err_pat in content:
                    return False, f"Validation error detected: '{err_pat}'"

        except Exception as e:
            logger.debug(f"Verification loop check error: {e}")

        await asyncio.sleep(0.5)

    try:
        submit_btn = page.locator('button[type="submit"]:visible, button:has-text("Submit"):visible, input[type="submit"]:visible').first
        if await submit_btn.count() > 0 and await submit_btn.is_visible():
            return False, "Submit button still visible on page; application was not accepted by portal."
    except Exception:
        pass

    return False, "Submission unconfirmed: No confirmation screen or redirect observed within timeout."


# Fixed, declared standard user-agent (no spoofing pool, no stealth tweaks per Section 5)
STANDARD_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
USER_AGENTS = [STANDARD_USER_AGENT]
STEALTH_SCRIPT = ""


class HITLManager:
    """
    Human-In-The-Loop (HITL) Async Lifecycle Manager.
    Enforces freeze -> ask -> resume -> verify flow for:
    - CAPTCHA_SOLVE_REQUEST (30-min window on Mac, solved/abandon)
    - MISSING_INFO_REQUEST (24-h window, user replies with field values)
    - HARD rule: Bot never touches a CAPTCHA widget.
    - Exactly one Telegram message per block per application, silence on timeout.
    """
    def __init__(self):
        self._pending_events: Dict[str, asyncio.Event] = {}
        self._results: Dict[str, Dict[str, Any]] = {}

    async def request_captcha_solve(
        self,
        job_id: str,
        company: str,
        role: str,
        portal: str,
        job_url: str,
        timeout_sec: float = 1800.0,
        screenshot_path: Optional[str] = None
    ) -> asyncio.Event:
        from app.services.telegram_notifier import send_telegram_alert
        event = asyncio.Event()
        self._pending_events[job_id] = event
        self._results[job_id] = {"action": "waiting", "data": None}

        buttons = [
            [
                {"text": "✅ Solved — Submit", "callback_data": f"hitl_solve|{job_id}"},
                {"text": "❌ Abandon", "callback_data": f"hitl_abandon|{job_id}"}
            ]
        ]
        await send_telegram_alert(
            job_id=job_id,
            alert_type="captcha_solve_request",
            company=company,
            role=role,
            portal=portal,
            job_url=job_url,
            details="Form 100% filled — solve the CAPTCHA on your Mac, then confirm below.",
            buttons=buttons,
            screenshot_path=screenshot_path,
            force=True
        )
        logger.info(f"[HITL A] CAPTCHA_SOLVE_REQUEST registered for {job_id} ({company}). Browser frozen on Mac.")
        return event

    async def request_missing_info(
        self,
        job_id: str,
        company: str,
        role: str,
        portal: str,
        job_url: str,
        missing_fields: List[str],
        timeout_sec: float = 86400.0,
        screenshot_path: Optional[str] = None
    ) -> asyncio.Event:
        from app.services.telegram_notifier import send_telegram_alert
        event = asyncio.Event()
        self._pending_events[job_id] = event
        self._results[job_id] = {"action": "waiting", "data": None}

        fields_bullet = "\n".join(f"• {f}" for f in missing_fields)
        details = f"Required field(s) with no resolvable value:\n{fields_bullet}\n\nPlease reply with values to fill and resume."
        buttons = [
            [
                {"text": "❌ Abandon", "callback_data": f"hitl_abandon|{job_id}"}
            ]
        ]
        await send_telegram_alert(
            job_id=job_id,
            alert_type="missing_info_request",
            company=company,
            role=role,
            portal=portal,
            job_url=job_url,
            details=details,
            buttons=buttons,
            screenshot_path=screenshot_path,
            force=True
        )
        logger.info(f"[HITL B] MISSING_INFO_REQUEST registered for {job_id} ({company}). Awaiting user reply.")
        return event

    def resolve_hitl_action(self, job_id: str, action: str = "solved", data: Any = None) -> bool:
        if job_id in self._pending_events:
            self._results[job_id] = {"action": action, "data": data}
            self._pending_events[job_id].set()
            logger.info(f"[HITL] Action '{action}' received for {job_id}.")
            return True
        return False

    async def wait_for_resolution(self, job_id: str, timeout_sec: float = 1800.0) -> Tuple[str, Any]:
        event = self._pending_events.get(job_id)
        if not event:
            return "abandon", None
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_sec)
            res = self._results.get(job_id, {"action": "abandon", "data": None})
            return res["action"], res["data"]
        except asyncio.TimeoutError:
            logger.info(f"[HITL] Window expired for {job_id} ({timeout_sec}s timeout).")
            return "timeout", None
        finally:
            self._pending_events.pop(job_id, None)
            self._results.pop(job_id, None)


hitl_manager = HITLManager()


def resolve_hitl_action(job_id: str, action: str = "solved", data: Any = None) -> bool:
    """Module-level bridge to resolve pending HITL requests from Telegram or dry-runs."""
    return hitl_manager.resolve_hitl_action(job_id, action, data)


class AutoFiller:
    """
    Advanced browser automation engine with anti-detection, 3 application modes,
    CAPTCHA detection, screenshot capture, and application logging.
    """

    def __init__(self):
        os.makedirs("data/screenshots", exist_ok=True)
        os.makedirs("data/browser_state", exist_ok=True)

    def validate_before_submit(self, result: dict) -> tuple[bool, list]:
        """Check if it's safe to submit the application."""
        reasons = []
        fields = result.get("fields_filled", {})
        
        if not fields.get("email") and not fields.get("first_name") and not fields.get("last_name") and not fields.get("full_name"):
            reasons.append("Missing required core fields (email/name)")
            
        from config import settings
        if hasattr(settings, "RESUME_PATH") and os.path.exists(settings.RESUME_PATH) and not result.get("resume_attached"):
            reasons.append("Resume not attached")
            
        if result.get("captcha_detected"):
            reasons.append("CAPTCHA blocking submission")
            
        if result.get("verification_pending"):
            reasons.append("Verification code required but not obtained")

        # Check for ungrounded or placeholder values
        for f_name, f_val in fields.items():
            if isinstance(f_val, str):
                v_clean = f_val.strip().lower()
                if v_clean in ["n/a", "na", "test", "--", "---", "placeholder", "undefined", "null", "todo"]:
                    reasons.append(f"Field '{f_name}' holds banned placeholder '{f_val}'")
            
        return (len(reasons) == 0, reasons)

    def clean_stale_screenshots(self, max_age_hours: int = 24, screenshot_dir: str = "data/screenshots"):
        """Clean up stale screenshots before starting an application run."""
        if not os.path.exists(screenshot_dir):
            return
        now = time.time()
        for fname in os.listdir(screenshot_dir):
            if fname.endswith(".png"):
                fpath = os.path.join(screenshot_dir, fname)
                try:
                    if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > (max_age_hours * 3600):
                        os.remove(fpath)
                except Exception:
                    pass

    def send_telegram_receipt(self, url: str, company: str, timestamp: str, screenshot_path: str, fields_count: int, status: str, reasons: list = None):
        """
        Send notification via Telegram Bot API using httpx.
        STRICT NOISE ELIMINATION:
        Telegram ONLY receives notifications for 'submitted' (verified proof) and 'capped' / 'cap_reached'.
        All skips, validation failures, timeouts, and errors are recorded in logs and the run digest ONLY.
        """
        from config import settings
        import httpx
        if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
            return

        # Suppress all Telegram noise for non-success and non-cap events
        if status not in ("submitted", "capped", "cap_reached", "skipped_cap_reached"):
            logger.info(f"Telegram alert suppressed for '{status}' on {company}. Recorded in logs and run digest.")
            return
            
        if status == "submitted":
            text = (
                f"✅ *Job Submitted Auto-Apply*\n"
                f"*Company:* {company}\n"
                f"*Time:* {timestamp}\n"
                f"*Fields Filled:* {fields_count}\n"
                f"*Policy:* Policy-based pacing applied\n"
                f"*URL:* {url}"
            )
        elif status in ("capped", "cap_reached", "skipped_cap_reached", "employer_limit", "candidate_limit"):
            reason_str = ", ".join(reasons) if reasons else "Per-company application limit active"
            is_employer_lock = "employer candidate limit" in reason_str.lower() or "candidate limit" in reason_str.lower()
            header_title = "Employer Candidate Limit" if is_employer_lock else "Per-Company Cap Reached"
            text = (
                f"🛑 *{header_title}*\n"
                f"*Company:* {company}\n"
                f"*Reason:* {reason_str}\n"
                f"*URL:* {url}"
            )
        else:
            return


        # Send asynchronously as a background task to avoid blocking
        async def _send():
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    if screenshot_path and os.path.exists(screenshot_path):
                        try:
                            with open(screenshot_path, "rb") as f:
                                files = {"photo": f}
                                data = {
                                    "chat_id": settings.TELEGRAM_CHAT_ID,
                                    "caption": text[:1024],
                                    "parse_mode": "Markdown"
                                }
                                resp = await client.post(
                                    f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendPhoto",
                                    data=data,
                                    files=files
                                )
                                if resp.status_code == 200:
                                    return
                        except Exception as pe:
                            logger.debug(f"Photo send error in send_telegram_receipt: {pe}")

                    await client.post(
                        f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": settings.TELEGRAM_CHAT_ID,
                            "text": text,
                            "parse_mode": "Markdown",
                            "disable_web_page_preview": True
                        }
                    )
            except Exception as e:
                logger.error(f"Failed to send Telegram receipt: {e}")

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_send())
        except RuntimeError as e:
            try:
                asyncio.run(_send())
            except Exception:
                pass
            logger.error(f"Telegram notification failed: {e}")

    # ── Policy-Based Deterministic Pacing ──────────────────────────────────

    async def field_pacing(self, el=None):
        """
        Deterministic policy-based pacing between field interactions.
        Waits for element stable state, then applies a fixed inter-field delay from config.
        No random delays, human-typing simulation, or variable stealth timing.
        """
        from config import settings
        if el is not None:
            try:
                if hasattr(el, "wait_for"):
                    await el.wait_for(state="visible", timeout=2000)
            except Exception:
                pass
        gap_ms = getattr(settings, "FIELD_FILL_GAP_MS", 600)
        with InactivityWatchdog.pause_current("field_pacing"):
            await asyncio.sleep(gap_ms / 1000.0)

    async def human_delay(self, min_s: float = 1.0, max_s: float = 4.0):
        """Deterministic policy pacing bridge (deprecated random delay)."""
        await self.field_pacing()

    async def human_type(self, target, selector: str, text: str):
        """Deterministic field fill with policy-based pacing (no random delays)."""
        return await self.safe_fill(target, selector, text)

    async def safe_fill(self, target, selector: str, value: str) -> bool:
        """Safely fill a field with deterministic policy-based pacing."""
        try:
            el = target.locator(selector).first
            if await el.count() > 0 and await el.is_visible():
                current_val = await el.input_value()
                if not current_val or not current_val.strip():
                    InactivityWatchdog.record_current_activity(f"typing: {selector}")
                    await el.fill(value)
                    await self.field_pacing(el)
                    return True
        except Exception:
            pass
        return False

    async def safe_select(self, target, selector: str, value: str) -> bool:
        """Safely select a dropdown option with deterministic policy-based pacing."""
        try:
            el = target.locator(selector).first
            if await el.count() > 0:
                tag = await el.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    try:
                        InactivityWatchdog.record_current_activity(f"select: {selector}")
                        await el.select_option(value=value)
                        await self.field_pacing(el)
                        return True
                    except Exception:
                        try:
                            InactivityWatchdog.record_current_activity(f"select: {selector}")
                            await el.select_option(label=value)
                            await self.field_pacing(el)
                            return True
                        except Exception:
                            pass
        except Exception:
            pass
        return False

    async def safe_click(self, target, selector: str, timeout: int = 3000) -> bool:
        """Attempt to click an element, return True if successful, with policy-based pacing."""
        try:
            el = target.locator(selector).first
            if await el.count() > 0 and await el.is_visible(timeout=timeout):
                InactivityWatchdog.record_current_activity(f"click: {selector}")
                await el.click()
                await self.field_pacing(el)
                return True
        except Exception:
            pass
        return False

    async def verify_submission(self, page, timeout_sec: float = 10.0) -> tuple[bool, str]:
        """Delegate to submission verification gate."""
        return await verify_submission(page, timeout_sec=timeout_sec)

    # ── Logging ──────────────────────────────────────────────────────────────

    def log_application(self, data: dict):
        """Append an application attempt to the JSON log file."""
        log_path = "data/application_log.json"
        try:
            with open(log_path, "a") as f:
                f.write(json.dumps(data, default=str) + "\n")
        except Exception as e:
            logger.error(f"Failed to write application log: {e}")

    # ── CAPTCHA Detection ────────────────────────────────────────────────────

    async def detect_captcha(self, page) -> bool:
        """Check for security challenges/CAPTCHAs across all signatures."""
        try:
            from app.services.presubmit_gate import SecurityChallengeDetector
            is_chal, _ = await SecurityChallengeDetector.detect_challenge(page)
            return is_chal
        except Exception:
            return False

    # ── Already Applied Detection ────────────────────────────────────────────

    async def detect_already_applied(self, page) -> bool:
        """Check if the user has already applied to this job."""
        try:
            content = await page.content() if callable(getattr(page, "content", None)) else ""
            if not isinstance(content, str):
                content = str(content)
            lower = content.lower()
            indicators = [
                "already applied", "you have applied",
                "application submitted", "previously applied",
                "you've already applied", "application received"
            ]
            return any(ind in lower for ind in indicators)
        except Exception:
            return False

    # ── Verification Code Detection & Automation ─────────────────────────────

    async def detect_verification_screen(self, page) -> bool:
        """Check if current page is requesting an email/SMS verification code."""
        code_input_selectors = [
            'input[autocomplete="one-time-code"]',
            'input[name*="verificationCode" i]',
            'input[name*="verification_code" i]',
            'input[name*="security_code" i]',
            'input[id*="verification" i]',
            'input[id*="passcode" i]',
            'input[placeholder*="verification code" i]',
            'input[placeholder*="enter code" i]',
            'input[aria-label*="verification code" i]',
        ]
        for sel in code_input_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    return True
            except Exception:
                continue

        try:
            content = (await page.content()).lower()
            indicators = [
                "enter the verification code",
                "enter verification code",
                "enter the code sent to",
                "one-time passcode",
                "security code sent to your email",
                "we sent a verification code",
                "check your email for a code",
            ]
            return any(ind in content for ind in indicators)
        except Exception:
            return False

    async def handle_verification_code_if_needed(self, page, portal: str) -> bool:
        """
        If a verification code is requested by the portal, poll Gmail via code_fetcher,
        fill in the code, and submit the verification step.
        """
        if not await self.detect_verification_screen(page):
            return True  # No verification needed

        logger.info(f"Verification code screen detected on {portal}. Awaiting code via Gmail...")
        from app.services.code_fetcher import wait_for_verification_code
        code = await wait_for_verification_code(sender_hint=portal, portal=portal, timeout=120)

        if not code:
            logger.warning(f"Verification code was not obtained for {portal}")
            return False

        # Attempt to fill code input
        code_selectors = [
            'input[autocomplete="one-time-code"]',
            'input[name*="verificationCode" i]',
            'input[name*="verification_code" i]',
            'input[name*="security_code" i]',
            'input[id*="verification" i]',
            'input[id*="passcode" i]',
            'input[placeholder*="code" i]',
            'input[aria-label*="code" i]',
            'input[type="text"]',
            'input[type="number"]',
        ]
        filled = False
        for sel in code_selectors:
            if await self.safe_fill(page, sel, code):
                logger.info(f"Entered verification code into selector: {sel}")
                filled = True
                break

        if filled:
            # Click Verify / Confirm / Continue button
            confirm_selectors = [
                'button:has-text("Verify")',
                'button:has-text("Confirm")',
                'button:has-text("Continue")',
                'button:has-text("Submit code")',
                'button[type="submit"]',
            ]
            for btn_sel in confirm_selectors:
                if await self.safe_click(page, btn_sel, timeout=2000):
                    logger.info(f"Clicked verification confirm button: {btn_sel}")
                    await self.human_delay(2.0, 4.0)
                    break
            return True

        return False

    # ── Screenshot ───────────────────────────────────────────────────────────

    async def take_screenshot(self, page, company: str = "unknown") -> str:
        """Take a screenshot of the current page state."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_company = "".join(c for c in company if c.isalnum() or c in (' ', '-', '_')).strip()[:30]
        screenshot_path = f"data/screenshots/{safe_company}_{timestamp}.png"
        try:
            await page.screenshot(path=screenshot_path, full_page=False)
            logger.info(f"Screenshot saved: {screenshot_path}")
            return screenshot_path
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            return ""

    # ── Form Filling ─────────────────────────────────────────────────────────

    async def fill_form_fields(self, page, profile: Profile) -> dict:
        """
        Fill all detectable form fields across main page and iframes.
        Returns dict of field_name -> True for each field filled.
        """
        fields_filled = {}

        first_name = profile.first_name or "Siddhesh"
        last_name = profile.last_name or "Sawant"
        full_name = profile.full_name or f"{first_name} {last_name}"
        email = profile.email or ""
        phone = profile.phone or ""
        linkedin = profile.linkedin_url or ""
        portfolio = profile.portfolio_url or ""
        github = profile.github_url or ""
        degree = profile.recent_degree or "Masters"
        school = profile.school or "George Mason University"
        company = profile.current_company or ""
        title = profile.current_title or ""
        city_state = profile.location or "Washington, DC"
        country = profile.country or "United States"

        # Try to find and click an "Apply" button first
        apply_buttons = [
            'a:has-text("Apply")', 'button:has-text("Apply")',
            'a:has-text("Apply now")', 'button:has-text("Apply now")',
            'a:has-text("Apply for this role")', 'button:has-text("Apply for this role")',
            'a:has-text("Apply for this job")', 'button:has-text("Apply for this job")',
            '#apply_button',
        ]
        for selector in apply_buttons:
            if await self.safe_click(page, selector, timeout=2000):
                await self.human_delay(2, 4)
                break

        # Check all frames (main page + embedded iframes)
        targets = [page] + page.frames

        for target in targets:
            try:
                # ── Standard ID-based Inputs ─────────────────────────────
                if await self.safe_fill(target, '#first_name', first_name):
                    fields_filled["first_name"] = True
                if await self.safe_fill(target, '#last_name', last_name):
                    fields_filled["last_name"] = True
                if await self.safe_fill(target, '#preferred_name', first_name):
                    fields_filled["preferred_name"] = True
                if await self.safe_fill(target, '#email', email):
                    fields_filled["email"] = True
                if await self.safe_fill(target, '#phone', phone):
                    fields_filled["phone"] = True
                if await self.safe_fill(target, '#candidate-location', city_state):
                    fields_filled["location"] = True

                # Greenhouse / Lever Experience & Education IDs
                if await self.safe_fill(target, '#company-name-0', company):
                    fields_filled["company"] = True
                if await self.safe_fill(target, '#title-0', title):
                    fields_filled["job_title"] = True
                if await self.safe_fill(target, '#school--0', school):
                    fields_filled["school"] = True
                if await self.safe_fill(target, '#degree--0', degree):
                    fields_filled["degree"] = True

                # Lever Standard Inputs
                if await self.safe_fill(target, 'input[name="name"]', full_name):
                    fields_filled["full_name"] = True
                if await self.safe_fill(target, 'input[name="email"]', email):
                    fields_filled["email"] = True
                if await self.safe_fill(target, 'input[name="phone"]', phone):
                    fields_filled["phone"] = True
                if await self.safe_fill(target, 'input[name="org"]', company):
                    fields_filled["company"] = True

                # Generic Input Selectors
                generic_fields = [
                    ('input[name*="firstName" i]', first_name, "first_name"),
                    ('input[name*="lastName" i]', last_name, "last_name"),
                    ('input[name*="email" i]', email, "email"),
                    ('input[type="email"]', email, "email"),
                    ('input[name*="phone" i]', phone, "phone"),
                    ('input[type="tel"]', phone, "phone"),
                    ('input[autocomplete="given-name"]', first_name, "first_name"),
                    ('input[autocomplete="family-name"]', last_name, "last_name"),
                    ('input[autocomplete="email"]', email, "email"),
                    ('input[autocomplete="tel"]', phone, "phone"),
                ]
                for sel, val, field_name in generic_fields:
                    if field_name not in fields_filled and val:
                        if await self.safe_fill(target, sel, val):
                            fields_filled[field_name] = True

                # Resume Upload
                try:
                    from config import settings
                    resume_file = profile.resume_path or getattr(settings, 'RESUME_PATH', None)
                except Exception:
                    resume_file = profile.resume_path

                if resume_file and os.path.exists(resume_file):
                    resume_selectors = [
                        '#_systemfield_resume',
                        'input[type="file"]#_systemfield_resume',
                        'input[type="file"][id*="resume" i]',
                        'input[type="file"][name*="resume" i]',
                        'input[type="file"][data-qa*="resume" i]',
                        '#resume',
                        'input[name="resume"]',
                        'input[type="file"]',
                    ]
                    for r_sel in resume_selectors:
                        try:
                            r_el = target.locator(r_sel).first
                            if await r_el.count() > 0:
                                with InactivityWatchdog.pause_current("resume_upload"):
                                    await r_el.set_input_files(resume_file)
                                InactivityWatchdog.record_current_activity("upload: resume")
                                fields_filled["resume"] = True
                                logger.info(f"Attached resume: {resume_file}")
                                break
                        except Exception:
                            continue

                # ── ARIA-Label and Placeholder Attribute Matching ───────
                try:
                    all_inputs = await target.locator('input, select, textarea').all()
                    for inp in all_inputs:
                        try:
                            aria = (await inp.get_attribute('aria-label') or '').lower()
                            placeholder = (await inp.get_attribute('placeholder') or '').lower()
                            name_attr = (await inp.get_attribute('name') or '').lower()
                            type_attr = (await inp.get_attribute('type') or '').lower()
                            combined_text = f"{aria} {placeholder} {name_attr}"

                            if not combined_text.strip():
                                continue

                            # LinkedIn
                            if 'linkedin' in combined_text and linkedin and 'linkedin' not in fields_filled:
                                if await self.safe_fill(target, f'[aria-label="{await inp.get_attribute("aria-label")}"]', linkedin):
                                    fields_filled["linkedin"] = True
                            # First Name
                            elif ('first name' in combined_text or 'given name' in combined_text) and 'first_name' not in fields_filled:
                                if await inp.fill(first_name):
                                    fields_filled["first_name"] = True
                            # Last Name
                            elif ('last name' in combined_text or 'family name' in combined_text) and 'last_name' not in fields_filled:
                                if await inp.fill(last_name):
                                    fields_filled["last_name"] = True
                            # Email
                            elif 'email' in combined_text and 'email' not in fields_filled:
                                if await inp.fill(email):
                                    fields_filled["email"] = True
                            # Phone
                            elif 'phone' in combined_text and 'phone' not in fields_filled:
                                if await inp.fill(phone):
                                    fields_filled["phone"] = True
                            # Company
                            elif 'company' in combined_text and company and 'company' not in fields_filled:
                                if await inp.fill(company):
                                    fields_filled["company"] = True
                            # Job Title
                            elif ('job title' in combined_text or 'title' in combined_text) and title and 'job_title' not in fields_filled:
                                if await inp.fill(title):
                                    fields_filled["job_title"] = True
                        except Exception:
                            continue
                except Exception:
                    pass

                # ── Label-Driven Field Matcher ───────────────────────────
                try:
                    labels = await target.locator('label').all()
                except Exception:
                    labels = []

                for lbl in labels:
                    try:
                        text = (await lbl.inner_text()).strip()
                        text_lower = text.lower()
                        for_attr = await lbl.get_attribute('for')
                        if not for_attr:
                            continue

                        el = target.locator(f'[id="{for_attr}"]').first
                        if await el.count() == 0:
                            continue

                        tag = await el.evaluate("el => el.tagName.toLowerCase()")
                        type_attr = (await el.get_attribute('type') or '').lower()
                        is_select = tag == "select"
                        is_text = type_attr in ['text', 'url', '']

                        # LinkedIn URL
                        if 'linkedin' in text_lower and is_text:
                            await el.fill(linkedin)
                            fields_filled["linkedin"] = True
                        # Portfolio/Website
                        elif ('portfolio' in text_lower or 'website' in text_lower) and is_text:
                            await el.fill(portfolio)
                            fields_filled["portfolio"] = True
                        # GitHub
                        elif 'github' in text_lower and is_text:
                            await el.fill(github)
                            fields_filled["github"] = True
                        # Degree / Education Level
                        elif ('degree' in text_lower or 'highest level of education' in text_lower):
                            if is_select:
                                await self.safe_select(target, f'[id="{for_attr}"]', degree)
                            elif is_text:
                                await el.click()
                                await target.keyboard.type(degree)
                                await target.keyboard.press('Enter')
                            fields_filled["degree"] = True
                        # School/University
                        elif ('school' in text_lower or 'university' in text_lower or 'college' in text_lower) and is_text:
                            await el.fill(school)
                            fields_filled["school"] = True
                        # City & State / Location
                        elif ('city' in text_lower and 'state' in text_lower) or 'reside' in text_lower:
                            if is_text:
                                await el.fill(city_state)
                                fields_filled["city_state"] = True
                        # Country
                        elif 'country' in text_lower:
                            if is_select:
                                await self.safe_select(target, f'[id="{for_attr}"]', country)
                            elif is_text:
                                await el.click()
                                await target.keyboard.type(country)
                                await target.keyboard.press('Enter')
                            fields_filled["country"] = True
                        # Current/Previous Employer
                        elif 'employer' in text_lower or 'current company' in text_lower:
                            if is_text:
                                await el.fill(company)
                                fields_filled["company"] = True
                        # Job Title
                        elif ('job title' in text_lower or text_lower == 'title') and is_text:
                            await el.fill(title)
                            fields_filled["job_title"] = True
                        # Work Authorization
                        elif 'authorized to work' in text_lower or 'legally authorized' in text_lower:
                            if is_select:
                                await self.safe_select(target, f'[id="{for_attr}"]', "Yes")
                            elif is_text:
                                await el.click()
                                await target.keyboard.type('Yes')
                                await target.keyboard.press('Enter')
                            elif type_attr == 'radio':
                                await el.check()
                            fields_filled["work_auth"] = True
                        # Sponsorship
                        elif 'sponsorship' in text_lower or 'visa status' in text_lower:
                            if is_select:
                                await self.safe_select(target, f'[id="{for_attr}"]', "Yes")
                            elif is_text:
                                await el.click()
                                await target.keyboard.type('Yes')
                                await target.keyboard.press('Enter')
                            elif type_attr == 'radio':
                                await el.check()
                            fields_filled["sponsorship"] = True
                        # Consent / Acknowledgement checkboxes
                        elif 'consent' in text_lower or 'acknowledge' in text_lower or 'agree' in text_lower:
                            if type_attr == 'checkbox':
                                await el.check()
                                fields_filled["consent"] = True

                    except Exception:
                        continue

            except Exception:
                continue

        return fields_filled

    # ── Multi-Step Form Navigation ───────────────────────────────────────────

    async def navigate_multi_step(self, page, profile: Profile, max_steps: int = 8) -> dict:
        """Handle multi-step application forms by clicking Next/Continue."""
        all_fields = {}
        for step in range(max_steps):
            # Mid-fill security challenge check (for steps > 0 after navigation)
            if step > 0:
                try:
                    from app.services.presubmit_gate import SecurityChallengeDetector
                    is_chal, chal_t = await SecurityChallengeDetector.detect_challenge(page)
                    if is_chal:
                        logger.warning(f"TERMINAL ABORT (mid_fill): Security challenge detected at step {step + 1}: {chal_t}")
                        all_fields["_challenge_detected"] = chal_t
                        all_fields["_challenge_stage"] = "mid_fill"
                        return all_fields
                except Exception as se:
                    logger.debug(f"Mid-fill security challenge check notice: {se}")

            await self.human_delay(1.5, 3.0)

            # Fill fields on current step
            step_fields = await self.fill_form_fields(page, profile)
            all_fields.update(step_fields)

            # Check for Submit/Review button → stop here
            stop_selectors = [
                'button[aria-label*="Submit application"]',
                'button:has-text("Submit application")',
                'button:has-text("Submit")',
                'button:has-text("Review your application")',
                'button:has-text("Review")',
            ]
            for sel in stop_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible(timeout=1000):
                        logger.info(f"Found submit/review button at step {step + 1}")
                        return all_fields
                except Exception:
                    continue

            # Look for Next/Continue button
            next_selectors = [
                'button[aria-label*="Continue to next step"]',
                'button[aria-label*="Next"]',
                'button:has-text("Next")',
                'button:has-text("Continue")',
                'button:has-text("Save and continue")',
            ]
            clicked_next = False
            for sel in next_selectors:
                if await self.safe_click(page, sel, timeout=2000):
                    logger.info(f"Step {step + 1}: Clicked next/continue")
                    clicked_next = True
                    await self.human_delay(2, 4)
                    break

            if clicked_next:
                # Re-check challenge after step progression
                try:
                    from app.services.presubmit_gate import SecurityChallengeDetector
                    is_chal, chal_t = await SecurityChallengeDetector.detect_challenge(page)
                    if is_chal:
                        logger.warning(f"TERMINAL ABORT (mid_fill): Security challenge appeared after next step click: {chal_t}")
                        all_fields["_challenge_detected"] = chal_t
                        all_fields["_challenge_stage"] = "mid_fill"
                        return all_fields
                except Exception:
                    pass
            else:
                break  # No more steps

        return all_fields

    # ── Main Entry Point ─────────────────────────────────────────────────────

    async def open_and_prefill(
        self,
        job_url: str,
        profile: Profile,
        mode: str = "review",
        job_title: Optional[str] = None,
        job_description: Optional[str] = None,
        company: Optional[str] = None,
        job_id: Optional[str] = None,
        hitl_enabled: bool = True,
        hitl_timeout_sec: Optional[float] = None
    ) -> dict:
        """
        Open live job application URL and pre-fill profile details.

        Modes:
            - 'review' (default): Fill form, screenshot, review ready, clean close
            - 'auto_submit': Fill form, screenshot, click submit, log, close
            - 'batch': Same as auto_submit but designed for sequential processing
        """
        current_job_id = job_id or f"hitl_{int(time.time()*1000)%1000000}"
        result = {
            "url": job_url,
            "opened": False,
            "fields_filled": {},
            "resume_attached": False,
            "message": "",
            "captcha_detected": False,
            "already_applied": False,
            "verification_screen_detected": False,
            "verification_pending": False,
            "screenshot_path": "",
            "success": False,
            "mode": mode,
            "status": "pending",
        }

        # Determine canonical company and portal
        portal_name = "direct"
        if not company:
            try:
                from app.services.company_cap import extract_ats_domain
                ats_slug = extract_ats_domain(job_url)
                if ":" in ats_slug:
                    parts = ats_slug.split(":", 1)
                    portal_name = parts[0]
                    company = parts[1].capitalize()
                elif ats_slug:
                    portal_name = ats_slug
                    from urllib.parse import urlparse
                    netloc_parts = urlparse(job_url).netloc.split('.')
                    company = netloc_parts[0].capitalize() if netloc_parts[0] not in ("jobs", "boards", "www") else (netloc_parts[1].capitalize() if len(netloc_parts) > 1 else "Target")
                else:
                    from urllib.parse import urlparse
                    netloc_parts = urlparse(job_url).netloc.split('.')
                    company = netloc_parts[0].capitalize() if netloc_parts[0] not in ("jobs", "boards", "www") else (netloc_parts[1].capitalize() if len(netloc_parts) > 1 else "Target")
            except Exception:
                company = "Target"
        else:
            try:
                from app.services.company_cap import extract_ats_domain
                ats_slug = extract_ats_domain(job_url)
                portal_name = ats_slug.split(":")[0] if ":" in ats_slug else (ats_slug or "direct")
            except Exception:
                portal_name = "direct"

        # ── Pre-Flight Hard Gates (Experience & Relevance) ────────────────────
        # Run hard gate BEFORE any browser or application work
        if job_title or job_description:
            from app.services.experience_gate import ExperienceGate
            from app.services.relevance_gate import RelevanceGate
            from app.services.audit_logger import audit_logger

            eval_title = job_title or "Software Engineer"
            eval_desc = job_description or ""

            # 1. Experience Gate: Apply ONLY to 0-3 years requirements
            exp_ok, exp_reason, exp_meta = ExperienceGate.evaluate(eval_title, eval_desc)
            if not exp_ok:
                result["status"] = "skipped"
                result["message"] = exp_reason
                logger.info(f"Pre-flight experience gate skipped: {exp_reason}")
                audit_logger.log_submission(
                    company=company,
                    role_title=eval_title,
                    job_url=job_url,
                    status="skipped",
                    skip_reason=exp_reason,
                    portal=portal_name
                )
                return result

            # 2. Relevance Gate: Material overlap with candidate stack only
            rel_ok, rel_reason, rel_meta = RelevanceGate.evaluate(eval_title, eval_desc)
            if not rel_ok:
                result["status"] = "skipped"
                result["message"] = rel_reason
                logger.info(f"Pre-flight relevance gate skipped: {rel_reason}")
                audit_logger.log_submission(
                    company=company,
                    role_title=eval_title,
                    job_url=job_url,
                    status="skipped",
                    skip_reason=rel_reason,
                    portal=portal_name
                )
                return result

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            result["message"] = "Playwright not installed. Run: pip install playwright && playwright install chromium"
            logger.error(result["message"])
            return result

        try:
            from config import settings
            self.clean_stale_screenshots()
            is_headed = getattr(settings, "DEBUG_HEADED", False)
            headless_mode = not is_headed
        except Exception:
            headless_mode = True

        context = None
        browser = None
        filler_watchdog = InactivityWatchdog(timeout_seconds=180.0, name=f"AutoFillerWatchdog-{company}")
        await filler_watchdog.__aenter__()
        try:
            from app.services.politeness import politeness
            portal_lock = politeness.get_portal_lock(job_url)

            async with portal_lock, async_playwright() as p:
                user_agent = STANDARD_USER_AGENT

                # Try persistent context launch first, fallback to standard launch on error
                try:
                    context = await p.chromium.launch_persistent_context(
                        user_data_dir="data/browser_state",
                        headless=headless_mode,
                        args=[
                            "--start-maximized",
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                        ],
                        user_agent=user_agent,
                        viewport={"width": 1280, "height": 900},
                    )
                except Exception as e:
                    logger.warning(f"Persistent context launch failed: {e}. Falling back to standard browser launch.")
                    browser = await p.chromium.launch(
                        headless=headless_mode,
                        args=[
                            "--start-maximized",
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                        ],
                    )
                    context = await browser.new_context(
                        user_agent=user_agent,
                        viewport={"width": 1280, "height": 900},
                    )

                page = await context.new_page()

                # Stealth mode
                await page.add_init_script(STEALTH_SCRIPT)

                # Clear Lever context before Lever application flows
                if "lever.co" in job_url.lower():
                    await clear_lever_context(context, page)

                # Navigate with retry
                for attempt in range(2):
                    try:
                        logger.info(f"Navigating to: {job_url} (attempt {attempt + 1})")
                        await page.goto(job_url, wait_until="domcontentloaded", timeout=45000)
                        InactivityWatchdog.record_current_activity(f"navigation: {job_url}")
                        result["opened"] = True
                        break
                    except Exception as e:
                        if attempt == 0:
                            logger.warning(f"Page load retry: {e}")
                            await self.human_delay(3, 5)
                        else:
                            raise

                await self.human_delay(2, 4)

                # ── Check Already Applied ────────────────────────────────
                if await self.detect_already_applied(page):
                    result["already_applied"] = True
                    result["message"] = "Already applied to this position."
                    self.log_application({
                        "timestamp": datetime.now().isoformat(),
                        "url": job_url, "mode": mode,
                        "status": "skipped_already_applied",
                    })
                    if mode != "review":
                        await context.close()
                    return result

                # ── Check CAPTCHA / Security Challenge (Pre-Fill) ────────
                try:
                    from app.services.presubmit_gate import SecurityChallengeDetector
                    from app.services.audit_logger import audit_logger
                    is_chal, chal_type = await SecurityChallengeDetector.detect_challenge(page)
                    if is_chal:
                        chal_shot = os.path.join("data/screenshots", f"challenge_{company}_{int(time.time())}.png")
                        try:
                            await page.screenshot(path=chal_shot, full_page=True)
                            result["screenshot_path"] = chal_shot
                        except Exception:
                            pass
                        skip_msg = f"CHALLENGE_BLOCKED: Security challenge detected before form fill: {chal_type}"
                        result["status"] = "security_challenge"
                        result["captcha_detected"] = True
                        result["challenge_type"] = chal_type
                        result["challenge_stage"] = "pre_fill"
                        result["message"] = skip_msg
                        logger.warning(f"TERMINAL ABORT (pre_fill): {skip_msg}")
                        audit_logger.log_submission(
                            company=company,
                            role_title=job_title or "Role",
                            job_url=job_url,
                            status="CHALLENGE_BLOCKED",
                            skip_reason=skip_msg,
                            portal=portal_name,
                            challenge_type=chal_type,
                            stage_detected="pre_fill",
                            screenshot_path=result.get("screenshot_path")
                        )
                        self.log_application({
                            "timestamp": datetime.now().isoformat(),
                            "url": job_url,
                            "company": company,
                            "mode": mode,
                            "status": "security_challenge",
                            "stage": "pre_fill",
                            "challenge_type": chal_type,
                            "screenshot_path": result.get("screenshot_path"),
                        })
                        # Clean immediate halt at ANY stage: zero interactive solves, close context
                        await context.close()
                        return result
                except Exception as ce:
                    logger.debug(f"Pre-fill security challenge check notice: {ce}")

                # ── Fill Form Fields (including multi-step) ──────────────
                result["fields_filled"] = await self.navigate_multi_step(page, profile)
                result["resume_attached"] = result["fields_filled"].get("resume", False)

                # ── Mid-Fill Security Challenge Halt Check ───────────────
                if result["fields_filled"].get("_challenge_detected"):
                    chal_type = result["fields_filled"].get("_challenge_detected")
                    chal_shot = os.path.join("data/screenshots", f"challenge_{company}_{int(time.time())}.png")
                    try:
                        await page.screenshot(path=chal_shot, full_page=True)
                        result["screenshot_path"] = chal_shot
                    except Exception:
                        pass
                    skip_msg = f"CHALLENGE_BLOCKED: Security challenge detected during form fill: {chal_type}"
                    result["status"] = "security_challenge"
                    result["captcha_detected"] = True
                    result["challenge_type"] = chal_type
                    result["challenge_stage"] = "mid_fill"
                    result["message"] = skip_msg
                    logger.warning(f"TERMINAL ABORT (mid_fill): {skip_msg}")
                    from app.services.audit_logger import audit_logger
                    audit_logger.log_submission(
                        company=company,
                        role_title=job_title or "Role",
                        job_url=job_url,
                        status="CHALLENGE_BLOCKED",
                        skip_reason=skip_msg,
                        portal=portal_name,
                        challenge_type=chal_type,
                        stage_detected="mid_fill",
                        screenshot_path=result.get("screenshot_path")
                    )
                    self.log_application({
                        "timestamp": datetime.now().isoformat(),
                        "url": job_url,
                        "company": company,
                        "mode": mode,
                        "status": "security_challenge",
                        "stage": "mid_fill",
                        "challenge_type": chal_type,
                        "screenshot_path": result.get("screenshot_path"),
                    })
                    await context.close()
                    return result

                filled_count = len(result["fields_filled"])
                logger.info(f"Pre-filled {filled_count} field(s) in {mode} mode")

                # ── Handle Verification Code Screens ─────────────────────
                if await self.detect_verification_screen(page):
                    result["verification_screen_detected"] = True
                    verified = await self.handle_verification_code_if_needed(page, portal_name)
                    if not verified:
                        result["verification_pending"] = True
                        logger.warning("Verification code requested but pending or timed out.")

                # ── Take Screenshot ──────────────────────────────────────
                result["screenshot_path"] = await self.take_screenshot(page, company)

                # ── Handle Mode ──────────────────────────────────────────
                if mode in ("auto_submit", "batch"):
                    # ── 1. Per-Company Application Cap Gate ──
                    try:
                        from app.services.company_cap import company_cap_tracker, is_employer_candidate_limit, extract_ats_domain
                        from app.services.audit_logger import audit_logger
                        is_capped, cap_reason, _ = company_cap_tracker.is_company_capped(company, job_url)
                        if is_capped:
                            result["status"] = "capped"
                            result["message"] = f"Per-company cap reached: {cap_reason}"
                            logger.warning(result["message"])
                            audit_logger.log_submission(company, job_title or "Role", job_url, status="skipped", skip_reason=cap_reason, portal=portal_name)
                            self.send_telegram_receipt(job_url, company, datetime.now().isoformat(), result["screenshot_path"], filled_count, "capped", [cap_reason])
                            self.log_application({
                                "timestamp": datetime.now().isoformat(),
                                "url": job_url,
                                "company": company,
                                "mode": mode,
                                "status": "capped",
                                "reasons": [cap_reason],
                            })
                            await context.close()
                            return result
                    except Exception as ce:
                        logger.debug(f"Company cap check error in auto_filler: {ce}")

                    # ── 2. Pre-Submit Security Challenge Check (Instant Zero-Retry Abort) ──
                    try:
                        from app.services.presubmit_gate import (
                            SecurityChallengeDetector,
                            OverlayChecker,
                            presubmit_gate
                        )
                        from app.services.audit_logger import audit_logger
                        is_challenge, challenge_type = await SecurityChallengeDetector.detect_challenge(page)
                        if is_challenge:
                            chal_shot = os.path.join("data/screenshots", f"challenge_{company}_{int(time.time())}.png")
                            try:
                                await page.screenshot(path=chal_shot, full_page=True)
                                result["screenshot_path"] = chal_shot
                            except Exception:
                                pass

                            # HITL A: CAPTCHA_SOLVE_REQUEST (Pre-submit + form filled + challenge detected)
                            # Freezes browser on Mac for 30-min window, sends ONE Telegram message with Solved/Abandon buttons
                            if hitl_enabled and mode in ("auto_submit", "batch") and filled_count > 0:
                                t_sec = hitl_timeout_sec or getattr(settings, "HITL_CAPTCHA_TIMEOUT_SEC", 1800.0)
                                logger.info(f"[HITL A] Pre-submit challenge ({challenge_type}) with {filled_count} fields filled. Entering freeze window ({t_sec}s) for {company}.")
                                result["captcha_solve_requested"] = True
                                result["challenge_type"] = challenge_type
                                result["state"] = "FROZEN_WAITING_CAPTCHA"

                                await hitl_manager.request_captcha_solve(
                                    job_id=current_job_id,
                                    company=company,
                                    role=job_title or "Software Engineer",
                                    portal=portal_name,
                                    job_url=job_url,
                                    timeout_sec=t_sec,
                                    screenshot_path=result.get("screenshot_path")
                                )

                                action, _ = await hitl_manager.wait_for_resolution(current_job_id, timeout_sec=t_sec)
                                if action == "solved":
                                    logger.info(f"[HITL A] Solved confirmed by user for {company}. Re-verifying fields...")
                                    result["captcha_solved"] = True
                                    result["hitl_resumed"] = True
                                    result["state"] = "READY_TO_SUBMIT"
                                    is_challenge = False
                                else:
                                    skip_msg = f"HUMAN_DEFERRED_TIMEOUT: CAPTCHA solve {action} after {t_sec}s window."
                                    result["status"] = "human_deferred_timeout"
                                    result["message"] = skip_msg
                                    logger.warning(f"[HITL A] Terminal deferral: {skip_msg}")
                                    audit_logger.log_submission(
                                        company=company,
                                        role_title=job_title or "Role",
                                        job_url=job_url,
                                        status="skipped",
                                        skip_reason=skip_msg,
                                        portal=portal_name
                                    )
                                    await context.close()
                                    return result

                            if is_challenge:
                                skip_msg = f"CHALLENGE_BLOCKED: Security challenge detected before submit ({challenge_type}); immediate clean abort without bypass attempt."
                                result["status"] = "security_challenge"
                                result["captcha_detected"] = True
                                result["challenge_type"] = challenge_type
                                result["challenge_stage"] = "pre_submit"
                                result["message"] = skip_msg
                                logger.warning(f"TERMINAL ABORT (pre_submit): {skip_msg}")
                                audit_logger.log_submission(
                                    company=company,
                                    role_title=job_title or "Role",
                                    job_url=job_url,
                                    status="CHALLENGE_BLOCKED",
                                    skip_reason=skip_msg,
                                    portal=portal_name,
                                    challenge_type=challenge_type,
                                    stage_detected="pre_submit",
                                    screenshot_path=result.get("screenshot_path")
                                )
                                self.log_application({
                                    "timestamp": datetime.now().isoformat(),
                                    "url": job_url,
                                    "company": company,
                                    "mode": mode,
                                    "fields_filled": filled_count,
                                    "status": "security_challenge",
                                    "stage": "pre_submit",
                                    "challenge_type": challenge_type,
                                    "reasons": [skip_msg],
                                    "screenshot_path": result.get("screenshot_path"),
                                })
                                await context.close()
                                return result
                    except Exception as se:
                        logger.debug(f"Security challenge pre-scan notice: {se}")

                    # ── 3. Completeness & Provenance Pre-Submit Validation Gate ──
                    is_valid, reasons = self.validate_before_submit(result)

                    try:
                        portal_slug = extract_ats_domain(job_url)
                        gate_result = await presubmit_gate.validate_page_state(
                            page,
                            company=company,
                            portal=portal_slug,
                            url=job_url
                        )
                        if not gate_result.is_valid:
                            is_valid = False
                            reasons.extend(gate_result.blocking_reasons)
                    except Exception as ge:
                        logger.warning(f"Presubmit gate validation notice: {ge}")

                    # Optional fallback completeness resolution
                    if is_valid and ensure_required_fields_filled:
                        profile_dict = {
                            "personal_info": {
                                "first_name": profile.first_name,
                                "last_name": profile.last_name,
                                "full_name": profile.full_name,
                                "email": profile.email,
                                "phone": profile.phone,
                            },
                            "education": [
                                {
                                    "school": getattr(profile, "school", "George Mason University") or "George Mason University",
                                    "degree": getattr(profile, "recent_degree", "Master's Degree") or "Master's Degree",
                                    "discipline": "Information Systems",
                                    "major": "Information Systems"
                                }
                            ],
                            "compliance_and_eeo": {
                                "work_auth_status": getattr(settings, "WORK_AUTH_STATUS", "Authorized / Require Sponsorship"),
                                "hybrid_office_willingness": getattr(settings, "HYBRID_OFFICE_WILLINGNESS", "Yes"),
                                "privacy_policy_consent": getattr(settings, "PRIVACY_POLICY_CONSENT", "Yes"),
                                "gender": "Male",
                                "race_ethnicity": "Asian",
                                "veteran_status": "I am not a protected veteran",
                                "disability_status": "No, I do not have a disability"
                            },
                            "compensation": {
                                "annual_salary_expectation": "100000",
                                "salary_expectation": "100k"
                            },
                            "preferences": {
                                "start_date": "Immediately"
                            }
                        }
                        req_ok, missing_reqs = await ensure_required_fields_filled(page, profile_dict)
                        if not req_ok:
                            missing_labels = [m.get("label", "Unknown") if isinstance(m, dict) else str(m) for m in (missing_reqs or [])][:4]
                            is_valid = False
                            reasons.append(f"Incomplete required fields: {', '.join(missing_labels)}")

                    # LLM Pre-Submit Verification Gate
                    if is_valid and llm_pre_submit_audit:
                        profile_dict = {
                            "personal_info": {
                                "first_name": profile.first_name,
                                "last_name": profile.last_name,
                                "full_name": profile.full_name,
                                "email": profile.email,
                                "phone": profile.phone,
                            },
                            "education": [
                                {
                                    "school": getattr(profile, "school", "George Mason University") or "George Mason University",
                                    "degree": getattr(profile, "recent_degree", "Master's Degree") or "Master's Degree",
                                    "discipline": "Information Systems",
                                    "major": "Information Systems"
                                }
                            ],
                            "compliance_and_eeo": {
                                "work_auth_status": getattr(settings, "WORK_AUTH_STATUS", "Authorized / Require Sponsorship"),
                                "hybrid_office_willingness": getattr(settings, "HYBRID_OFFICE_WILLINGNESS", "Yes"),
                                "privacy_policy_consent": getattr(settings, "PRIVACY_POLICY_CONSENT", "Yes"),
                                "gender": "Male",
                                "race_ethnicity": "Asian",
                                "veteran_status": "I am not a protected veteran",
                                "disability_status": "No, I do not have a disability"
                            },
                            "compensation": {
                                "annual_salary_expectation": "100000",
                                "salary_expectation": "100k"
                            },
                            "preferences": {
                                "start_date": "Immediately"
                            }
                        }
                        try:
                            from app.services.llm_verifier import llm_pre_submit_audit as _verifier_audit
                            audit_call = _verifier_audit if _verifier_audit is not None else llm_pre_submit_audit
                            pre_audit_ok, pre_audit_reason, pre_audit_issues = await audit_call(page, profile_dict, result["screenshot_path"])
                            if not pre_audit_ok:
                                is_valid = False
                                reasons.append(f"LLM Pre-Submit Audit: {pre_audit_reason}")
                        except Exception as pae:
                            logger.error(f"LLM pre-submit audit error: {pae}")

                    if not is_valid:
                        # HITL B: MISSING_INFO_REQUEST (Required fields with no resolvable value -> freeze, 24-h window)
                        has_missing_reqs = any(
                            "Incomplete required fields" in r or
                            "Required selection" in r or
                            "Required input" in r or
                            "required" in r.lower() or
                            "not selected" in r.lower() or
                            "empty" in r.lower()
                            for r in reasons
                        )
                        if hitl_enabled and mode in ("auto_submit", "batch") and has_missing_reqs:
                            missing_field_names = []
                            for r in reasons:
                                if "Incomplete required fields:" in r:
                                    items = r.replace("Incomplete required fields:", "").strip()
                                    missing_field_names.extend([f.strip() for f in items.split(",") if f.strip()])
                                else:
                                    matches = re.findall(r"Required\s+(?:selection|input|field|question)?\s*['\"]([^'\"]+)['\"]", r, re.IGNORECASE)
                                    if matches:
                                        missing_field_names.extend(matches)
                            if not missing_field_names and 'pre_audit_issues' in locals() and pre_audit_issues:
                                for issue in pre_audit_issues:
                                    matches = re.findall(r"Required\s+(?:selection|input|field|question)?\s*['\"]([^'\"]+)['\"]", issue, re.IGNORECASE)
                                    if matches:
                                        missing_field_names.extend(matches)
                                    else:
                                        missing_field_names.append(issue)
                            if not missing_field_names:
                                missing_field_names = ["Required Application Field"]
                            seen_fn = set()
                            missing_field_names = [f for f in missing_field_names if not (f in seen_fn or seen_fn.add(f))]
                            t_sec = hitl_timeout_sec or getattr(settings, "HITL_MISSING_INFO_TIMEOUT_SEC", 86400.0)
                            logger.info(f"[HITL B] Required fields missing with no resolvable value: {missing_field_names}. Entering freeze window ({t_sec}s) for {company}.")
                            result["missing_info_requested"] = True
                            result["missing_fields"] = missing_field_names
                            result["state"] = "FROZEN_WAITING_INFO"

                            await hitl_manager.request_missing_info(
                                job_id=current_job_id,
                                company=company,
                                role=job_title or "Software Engineer",
                                portal=portal_name,
                                job_url=job_url,
                                missing_fields=missing_field_names,
                                timeout_sec=t_sec,
                                screenshot_path=result.get("screenshot_path")
                            )

                            action, reply_data = await hitl_manager.wait_for_resolution(current_job_id, timeout_sec=t_sec)
                            if action == "info_reply" and reply_data:
                                logger.info(f"[HITL B] User supplied values: {reply_data}. Filling verbatim with fixed pacing...")
                                if isinstance(reply_data, dict):
                                    for f_name, f_val in reply_data.items():
                                        await self.safe_fill(page, f"input[name*='{f_name}' i], textarea[name*='{f_name}' i]", str(f_val))
                                        try:
                                            from app.services.common_fields import common_field_resolver
                                            common_field_resolver.save_learned_field(f_name, str(f_val))
                                        except Exception:
                                            pass
                                elif isinstance(reply_data, str) and missing_field_names:
                                    target_field = missing_field_names[0]
                                    await self.safe_fill(page, f"input[name*='{target_field}' i], textarea[name*='{target_field}' i]", reply_data)
                                    try:
                                        from app.services.common_fields import common_field_resolver
                                        common_field_resolver.save_learned_field(target_field, reply_data)
                                    except Exception:
                                        pass

                                # Re-verify fields
                                is_valid = True
                                reasons = []
                                result["hitl_resumed"] = True

                                # Check if CAPTCHA now appears (transitions into A if CAPTCHA appears, else submit)
                                is_chal_now, chal_t_now = await SecurityChallengeDetector.detect_challenge(page)
                                if is_chal_now:
                                    logger.info(f"[HITL B->A] Security challenge ({chal_t_now}) appeared after filling missing info. Transitioning to HITL A.")
                                    t_cap_sec = hitl_timeout_sec or getattr(settings, "HITL_CAPTCHA_TIMEOUT_SEC", 1800.0)
                                    result["captcha_solve_requested"] = True
                                    result["state"] = "FROZEN_WAITING_CAPTCHA"
                                    await hitl_manager.request_captcha_solve(
                                        job_id=current_job_id,
                                        company=company,
                                        role=job_title or "Software Engineer",
                                        portal=portal_name,
                                        job_url=job_url,
                                        timeout_sec=t_cap_sec,
                                        screenshot_path=result.get("screenshot_path")
                                    )
                                    cap_act, _ = await hitl_manager.wait_for_resolution(current_job_id, timeout_sec=t_cap_sec)
                                    if cap_act == "solved":
                                        result["captcha_solved"] = True
                                        result["state"] = "READY_TO_SUBMIT"
                                    else:
                                        skip_msg = f"HUMAN_DEFERRED_TIMEOUT: CAPTCHA solve {cap_act} after {t_cap_sec}s window."
                                        result["status"] = "human_deferred_timeout"
                                        result["message"] = skip_msg
                                        audit_logger.log_submission(company, job_title or "Role", job_url, status="skipped", skip_reason=skip_msg, portal=portal_name)
                                        await context.close()
                                        return result
                                else:
                                    result["state"] = "READY_TO_SUBMIT"
                            else:
                                skip_msg = f"HUMAN_DEFERRED_TIMEOUT: Missing info {action} after {t_sec}s window."
                                result["status"] = "human_deferred_timeout"
                                result["message"] = skip_msg
                                audit_logger.log_submission(company, job_title or "Role", job_url, status="skipped", skip_reason=skip_msg, portal=portal_name)
                                await context.close()
                                return result

                    if not is_valid:
                        result["status"] = "skipped"
                        result["message"] = "Validation blocked before submit: " + ", ".join(reasons)
                        logger.warning(result["message"])
                        audit_logger.log_submission(company, job_title or "Role", job_url, status="skipped", skip_reason=result["message"], portal=portal_name)
                        self.log_application({
                            "timestamp": datetime.now().isoformat(),
                            "url": job_url,
                            "company": company,
                            "mode": mode,
                            "fields_filled": filled_count,
                            "status": "skipped",
                            "reasons": reasons,
                        })
                        # Strict noise elimination: NO Telegram notification on validation failure
                        await context.close()
                        return result

                    # ── 4. Submit Button Resolution & Pre-Click Overlay Check ──
                    submit_selectors = [
                        'button[type="submit"]',
                        'button:has-text("Submit application")',
                        'button:has-text("Submit")',
                        'button[aria-label*="Submit"]',
                        'input[type="submit"]',
                    ]
                    target_submit_sel = None
                    for sel in submit_selectors:
                        try:
                            loc = page.locator(sel).first
                            if await loc.count() > 0 and await loc.is_visible():
                                target_submit_sel = sel
                                break
                        except Exception:
                            continue

                    clicked_submit = False
                    if target_submit_sel:
                        # Pre-click elementFromPoint overlay check
                        can_click, obs_reason = await OverlayChecker.verify_submit_interactable(page, target_submit_sel)
                        if not can_click:
                            skip_msg = f"Submit blocked by un-dismissible overlay: {obs_reason}"
                            result["status"] = "skipped"
                            result["message"] = skip_msg
                            logger.warning(skip_msg)
                            audit_logger.log_submission(company, job_title or "Role", job_url, status="skipped", skip_reason=skip_msg, portal=portal_name)
                            self.log_application({
                                "timestamp": datetime.now().isoformat(),
                                "url": job_url,
                                "company": company,
                                "mode": mode,
                                "fields_filled": filled_count,
                                "status": "skipped",
                                "reasons": [skip_msg],
                            })
                            await context.close()
                            return result

                        # Bounded submit click attempt (max 2 attempts)
                        for attempt in range(2):
                            if await self.safe_click(page, target_submit_sel, timeout=3000):
                                clicked_submit = True
                                logger.info(f"Clicked submit button via: {target_submit_sel} (attempt {attempt + 1})")
                                break
                            await asyncio.sleep(0.5)

                    submitted = False
                    verify_reason = ""
                    is_post_chal = False
                    chal_t = ""
                    if not clicked_submit:
                        verify_reason = f"Filled {filled_count} fields but submit button not clickable."
                        result["status"] = "skipped"
                        result["message"] = verify_reason
                        logger.warning(result["message"])
                        audit_logger.log_submission(company, job_title or "Role", job_url, status="skipped", skip_reason=verify_reason, portal=portal_name)
                    else:
                        # Post-click security challenge check
                        is_post_chal, chal_t = await SecurityChallengeDetector.detect_challenge(page)
                        if is_post_chal:
                            verify_reason = f"Post-submit security challenge appeared ({chal_t})"
                            submitted = False
                        else:
                            # ── 5. Post-Submit Verification Gate ──
                            if llm_post_submit_verify:
                                try:
                                    verified, verify_reason = await llm_post_submit_verify(page, result["screenshot_path"], initial_url=job_url, timeout_sec=10.0)
                                except Exception as pe:
                                    logger.error(f"Error in llm_post_submit_verify: {pe}")
                                    verified, verify_reason = await verify_submission(page, timeout_sec=6.0)
                            else:
                                verified, verify_reason = await verify_submission(page, timeout_sec=10.0)

                            submitted = verified

                            # Check if challenge was detected during verify_submission polling or mounted late
                            if not submitted and not is_post_chal:
                                if "security challenge" in verify_reason.lower():
                                    is_post_chal = True
                                    chal_t = verify_reason
                                else:
                                    # Secondary check for late-mounting challenge iframes (e.g. Ashby/Ramp/Waabi)
                                    is_late_chal, late_chal_t = await SecurityChallengeDetector.detect_challenge(page)
                                    if is_late_chal:
                                        is_post_chal = True
                                        chal_t = late_chal_t
                                        verify_reason = f"Post-submit security challenge appeared ({chal_t})"

                    # Take fresh screenshot of the final page state
                    result["screenshot_path"] = await self.take_screenshot(page, company)

                    if submitted:
                        result["status"] = "submitted"
                        result["success"] = True
                        result["message"] = f"Application verified and submitted! ({verify_reason})"
                        try:
                            from app.services.company_cap import company_cap_tracker
                            from app.services.audit_logger import audit_logger
                            company_cap_tracker.record_submission(company, job_title or "Role", job_url, status="submitted", match_reason="Auto-applied")
                            audit_logger.log_submission(company, job_title or "Role", job_url, match_reason="Auto-applied", match_score=1.0, status="submitted", portal=portal_name)
                        except Exception as le:
                            logger.debug(f"Cap tracker logging error: {le}")

                        self.send_telegram_receipt(
                            job_url,
                            company,
                            datetime.now().isoformat(),
                            result["screenshot_path"],
                            filled_count,
                            "submitted"
                        )
                    else:
                        result["success"] = False
                        if is_post_chal:
                            result["status"] = "security_challenge"
                            result["captcha_detected"] = True
                            result["challenge_type"] = chal_t
                            result["challenge_stage"] = "post_submit"
                            result["message"] = f"Post-submit security challenge detected: {chal_t}"
                            logger.warning(f"TERMINAL ABORT (post_submit): {result['message']}")
                            try:
                                from app.services.audit_logger import audit_logger
                                audit_logger.log_submission(
                                    company=company,
                                    role_title=job_title or "Role",
                                    job_url=job_url,
                                    status="CHALLENGE_BLOCKED",
                                    skip_reason=result["message"],
                                    portal=portal_name,
                                    challenge_type=chal_t,
                                    stage_detected="post_submit",
                                    screenshot_path=result.get("screenshot_path")
                                )
                            except Exception as le:
                                logger.debug(f"Audit logger challenge notice: {le}")
                        # Check specific employer candidate limit pattern
                        elif is_employer_candidate_limit(verify_reason):
                            result["status"] = "capped"
                            result["message"] = f"Employer candidate limit reached: {verify_reason}"
                            try:
                                company_cap_tracker.record_employer_limit_reached(company, job_url, limit_days=180, reason=verify_reason)
                                audit_logger.log_submission(company, job_title or "Role", job_url, match_reason="Auto-applied", match_score=1.0, status="skipped", skip_reason=verify_reason, portal=portal_name)
                            except Exception as le:
                                logger.debug(f"Cap tracker logging error: {le}")
                            self.send_telegram_receipt(
                                job_url,
                                company,
                                datetime.now().isoformat(),
                                result["screenshot_path"],
                                filled_count,
                                "capped",
                                [verify_reason]
                            )
                        else:
                            # Clean skip with zero telegram noise
                            result["status"] = "skipped"
                            result["message"] = f"Application skipped (unverified): {verify_reason}"
                            logger.warning(result["message"])
                            audit_logger.log_submission(company, job_title or "Role", job_url, match_reason="Auto-applied", match_score=1.0, status="skipped", skip_reason=verify_reason, portal=portal_name)

                    log_data = {
                        "timestamp": datetime.now().isoformat(),
                        "url": job_url,
                        "company": company,
                        "mode": mode,
                        "fields_filled": filled_count,
                        "resume_attached": result["resume_attached"],
                        "submitted": submitted,
                        "screenshot": result["screenshot_path"],
                        "status": result["status"],
                        "verification_reason": verify_reason,
                    }
                    if is_post_chal:
                        log_data["stage"] = "post_submit"
                        log_data["challenge_type"] = chal_t
                    self.log_application(log_data)

                    await context.close()


                else:
                    # review mode: form filled, screenshot captured, clean close (no manual solve wait)
                    result["message"] = (
                        f"Browser opened! Pre-filled {filled_count} field(s). "
                        f"{'Resume attached! ' if result['resume_attached'] else ''}"
                        f"Form filled and screenshot captured."
                    )
                    result["success"] = True
                    result["status"] = "review_ready"

                    self.log_application({
                        "timestamp": datetime.now().isoformat(),
                        "url": job_url,
                        "company": company,
                        "mode": "review",
                        "fields_filled": filled_count,
                        "screenshot": result["screenshot_path"],
                        "status": "review_ready",
                    })

                    logger.info(result["message"])
                    await context.close()

        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError) and not filler_watchdog.timed_out:
                raise
            skip_msg = "Global watchdog timeout exceeded (180s); safe abort."
            result["status"] = "skipped"
            result["message"] = skip_msg
            logger.warning(skip_msg)
            try:
                from app.services.audit_logger import audit_logger
                audit_logger.log_submission(company, "Role", job_url, status="skipped", skip_reason=skip_msg)
            except Exception:
                pass
            self.log_application({
                "timestamp": datetime.now().isoformat(),
                "url": job_url,
                "company": company,
                "mode": mode,
                "status": "skipped",
                "reasons": [skip_msg],
            })
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

        except Exception as e:
            skip_msg = f"Auto-fill exception: {str(e)}"
            result["status"] = "skipped"
            result["message"] = skip_msg
            result["error"] = str(e)
            logger.error(skip_msg)
            try:
                from app.services.audit_logger import audit_logger
                audit_logger.log_submission(company, "Role", job_url, status="skipped", skip_reason=skip_msg)
            except Exception:
                pass
            self.log_application({
                "timestamp": datetime.now().isoformat(),
                "url": job_url,
                "company": company,
                "mode": mode,
                "status": "skipped",
                "error": str(e),
                "reasons": [skip_msg],
            })

            if context:
                try:
                    await context.close()
                except Exception:
                    pass

        finally:
            await filler_watchdog.__aexit__(None, None, None)

        return result


auto_filler = AutoFiller()


