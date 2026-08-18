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
from datetime import datetime
from app.models import Profile

logger = logging.getLogger(__name__)

# Realistic Chrome user-agent pool for macOS
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

# Stealth JS to hide webdriver flag
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = {runtime: {}};
"""


class AutoFiller:
    """
    Advanced browser automation engine with anti-detection, 3 application modes,
    CAPTCHA detection, screenshot capture, and application logging.
    """

    def __init__(self):
        os.makedirs("data/screenshots", exist_ok=True)
        os.makedirs("data/browser_state", exist_ok=True)

    # ── Human-like Behavior ──────────────────────────────────────────────────

    async def human_delay(self, min_s: float = 1.0, max_s: float = 4.0):
        """Sleep for a random human-like duration."""
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def human_type(self, target, selector: str, text: str):
        """Type text character-by-character with random delays to mimic a human."""
        try:
            el = target.locator(selector).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill("")
                for char in text:
                    await el.type(char, delay=random.randint(50, 150))
                await self.human_delay(0.3, 0.8)
                return True
        except Exception:
            pass
        return False

    async def safe_fill(self, target, selector: str, value: str) -> bool:
        """Safely fill a field, return True if successful."""
        try:
            el = target.locator(selector).first
            if await el.count() > 0 and await el.is_visible():
                current_val = await el.input_value()
                if not current_val or not current_val.strip():
                    await el.fill(value)
                    await self.human_delay(0.3, 0.6)
                    return True
        except Exception:
            pass
        return False

    async def safe_select(self, target, selector: str, value: str) -> bool:
        """Safely select a dropdown option by value or label."""
        try:
            el = target.locator(selector).first
            if await el.count() > 0:
                tag = await el.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    try:
                        await el.select_option(value=value)
                        return True
                    except Exception:
                        try:
                            await el.select_option(label=value)
                            return True
                        except Exception:
                            pass
        except Exception:
            pass
        return False

    async def safe_click(self, target, selector: str, timeout: int = 3000) -> bool:
        """Attempt to click an element, return True if successful."""
        try:
            el = target.locator(selector).first
            if await el.count() > 0 and await el.is_visible(timeout=timeout):
                await self.human_delay(0.3, 0.8)
                await el.click()
                return True
        except Exception:
            pass
        return False

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
        """Check for common CAPTCHA indicators on the page."""
        captcha_selectors = [
            "iframe[src*='captcha']",
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "[id*='captcha']",
            "[class*='captcha']",
            "[id*='recaptcha']",
            "[class*='g-recaptcha']",
            "[data-sitekey]",
        ]
        for sel in captcha_selectors:
            try:
                if await page.locator(sel).count() > 0:
                    logger.warning(f"CAPTCHA detected via selector: {sel}")
                    return True
            except Exception:
                continue
        return False

    # ── Already Applied Detection ────────────────────────────────────────────

    async def detect_already_applied(self, page) -> bool:
        """Check if the user has already applied to this job."""
        try:
            content = await page.content()
            lower = content.lower()
            indicators = [
                "already applied", "you have applied",
                "application submitted", "previously applied",
                "you've already applied", "application received"
            ]
            return any(ind in lower for ind in indicators)
        except Exception:
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
                    resume_selectors = ['#resume', 'input[name="resume"]', 'input[type="file"]']
                    for r_sel in resume_selectors:
                        try:
                            r_el = target.locator(r_sel).first
                            if await r_el.count() > 0:
                                await r_el.set_input_files(resume_file)
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

            if not clicked_next:
                break  # No more steps

        return all_fields

    # ── Main Entry Point ─────────────────────────────────────────────────────

    async def open_and_prefill(self, job_url: str, profile: Profile, mode: str = "review") -> dict:
        """
        Open live job application URL and pre-fill profile details.

        Modes:
            - 'review' (default): Fill form, screenshot, keep browser open for user
            - 'auto_submit': Fill form, screenshot, click submit, log, close
            - 'batch': Same as auto_submit but designed for sequential processing
        """
        result = {
            "url": job_url,
            "opened": False,
            "fields_filled": {},
            "resume_attached": False,
            "message": "",
            "captcha_detected": False,
            "already_applied": False,
            "screenshot_path": "",
            "success": False,
            "mode": mode,
        }

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            result["message"] = "Playwright not installed. Run: pip install playwright && playwright install chromium"
            logger.error(result["message"])
            return result

        context = None
        browser = None
        try:
            async with async_playwright() as p:
                user_agent = random.choice(USER_AGENTS)

                # Try persistent context launch first, fallback to standard launch on error
                try:
                    context = await p.chromium.launch_persistent_context(
                        user_data_dir="data/browser_state",
                        headless=False,
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
                        headless=False,
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

                # Navigate with retry
                for attempt in range(2):
                    try:
                        logger.info(f"Navigating to: {job_url} (attempt {attempt + 1})")
                        await page.goto(job_url, wait_until="domcontentloaded", timeout=45000)
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

                # ── Check CAPTCHA ────────────────────────────────────────
                if await self.detect_captcha(page):
                    result["captcha_detected"] = True
                    result["message"] = "CAPTCHA detected — manual intervention required."
                    self.log_application({
                        "timestamp": datetime.now().isoformat(),
                        "url": job_url, "mode": mode,
                        "status": "captcha_detected",
                    })
                    if mode == "review":
                        # Keep browser open so user can solve CAPTCHA
                        await page.wait_for_timeout(300000)
                    else:
                        await context.close()
                    return result

                # ── Fill Form Fields (including multi-step) ──────────────
                result["fields_filled"] = await self.navigate_multi_step(page, profile)
                result["resume_attached"] = result["fields_filled"].get("resume", False)

                filled_count = len(result["fields_filled"])
                logger.info(f"Pre-filled {filled_count} field(s) in {mode} mode")

                # ── Take Screenshot ──────────────────────────────────────
                try:
                    from urllib.parse import urlparse
                    company = urlparse(job_url).netloc.split('.')[0]
                except Exception:
                    company = "unknown"
                result["screenshot_path"] = await self.take_screenshot(page, company)

                # ── Handle Mode ──────────────────────────────────────────
                if mode in ("auto_submit", "batch"):
                    # Find and click submit button
                    submit_selectors = [
                        'button[type="submit"]',
                        'button:has-text("Submit application")',
                        'button:has-text("Submit")',
                        'button[aria-label*="Submit"]',
                        'input[type="submit"]',
                    ]
                    submitted = False
                    for sel in submit_selectors:
                        if await self.safe_click(page, sel, timeout=3000):
                            submitted = True
                            result["success"] = True
                            logger.info(f"Application submitted via: {sel}")
                            await self.human_delay(3, 5)
                            break

                    if not submitted:
                        result["message"] = f"Filled {filled_count} fields but could not find submit button."
                        logger.warning(result["message"])
                    else:
                        result["message"] = f"Application submitted! Filled {filled_count} fields."

                    self.log_application({
                        "timestamp": datetime.now().isoformat(),
                        "url": job_url,
                        "company": company,
                        "mode": mode,
                        "fields_filled": filled_count,
                        "resume_attached": result["resume_attached"],
                        "submitted": submitted,
                        "screenshot": result["screenshot_path"],
                        "status": "submitted" if submitted else "submit_failed",
                    })

                    await context.close()

                else:
                    # review mode — keep browser open
                    result["message"] = (
                        f"Browser opened! Pre-filled {filled_count} field(s). "
                        f"{'Resume attached! ' if result['resume_attached'] else ''}"
                        f"Review the form and click Submit manually."
                    )
                    result["success"] = True

                    self.log_application({
                        "timestamp": datetime.now().isoformat(),
                        "url": job_url,
                        "company": company,
                        "mode": "review",
                        "fields_filled": filled_count,
                        "screenshot": result["screenshot_path"],
                        "status": "review_paused",
                    })

                    logger.info(result["message"])
                    # Keep browser open for 5 minutes
                    await page.wait_for_timeout(300000)

        except Exception as e:
            result["message"] = f"Auto-fill notice: {str(e)}"
            result["error"] = str(e)
            logger.error(f"Auto-fill exception: {e}")

            self.log_application({
                "timestamp": datetime.now().isoformat(),
                "url": job_url,
                "mode": mode,
                "status": "error",
                "error": str(e),
            })

            if context:
                try:
                    await context.close()
                except Exception:
                    pass

        return result
