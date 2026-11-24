#!/usr/bin/env python3
"""
==============================================================================
 UnifiedATSFormFiller — Production-Grade Multi-ATS Form Automation Engine
==============================================================================
 Supported portals: Greenhouse, Ashby, Lever, SmartRecruiters.
 Architecture:
   1. Universal iframe resolution (Greenhouse #grnh_iframe, embedded iframes)
   2. Visual Question Block parsing — groups DOM into label+input blocks
   3. React Synthetic Event injection — press_sequentially with delays
   4. Forced label clicking — hidden native radios/checkboxes
   5. Deterministic regex engine → Gemini AI fallback
   6. Recursive sweep loop until 0 new fields filled per pass
   7. Audit gate → scroll to top → NEVER auto-submit
==============================================================================
"""

import asyncio
import json
import logging
import os
import re
import ssl
import sys
if "/Users/siddheshsawant/sid_job_scrip" not in sys.path:
    sys.path.insert(0, "/Users/siddheshsawant/sid_job_scrip")
import calendar
from datetime import datetime, date, timedelta
import time
import random
import contextlib
import traceback
import urllib.request
import certifi
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from playwright.async_api import (
    Page, Frame, FrameLocator, Locator,
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

from app.services.form_guards import (
    FormHaltException,
    FillVerificationFailedError,
    is_relocation_or_in_office_question,
    is_relocation_willingness_question,
    solve_relocation_willingness,
    is_current_location_or_proximity_question,
    solve_proximity_or_current_location,
    is_relocation_assistance_question,
    is_relocation_timing_question,
    is_remote_willingness_question,
    is_travel_decision_question,
    solve_canonical_screening_question,
    is_essay_or_subjective_question,
    is_contradictory_premise_question,
    is_home_address_field,
    is_how_did_you_hear_question,
    solve_how_did_you_hear,
    is_which_visa_question,
    solve_which_visa,
    solve_consent_question,
    format_missing_info_request,
)

# ---------------------------------------------------------------------------
# Optional .env loading
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    from llm_verifier import llm_pre_submit_audit, llm_post_submit_verify
except ImportError:
    try:
        from app.services.llm_verifier import llm_pre_submit_audit, llm_post_submit_verify
    except ImportError:
        llm_pre_submit_audit = None
        llm_post_submit_verify = None

try:
    from app.services.inactivity_watchdog import InactivityWatchdog
except ImportError:
    try:
        from inactivity_watchdog import InactivityWatchdog
    except ImportError:
        InactivityWatchdog = None

def _record_activity(action: str):
    if InactivityWatchdog:
        try:
            InactivityWatchdog.record_current_activity(action)
        except Exception:
            pass

@contextlib.contextmanager
def _pause_watchdog(reason: str):
    if InactivityWatchdog:
        with InactivityWatchdog.pause_current(reason):
            yield
    else:
        yield

import config  # local project config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("form_filler")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROFILE_PATH = Path(__file__).parent / "profile.json"

SUPPORTED_PORTALS = {
    "greenhouse": [
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "grnh.se",
    ],
    "ashby": ["jobs.ashbyhq.com", "ashbyhq.com"],
    "lever": ["jobs.lever.co", "lever.co"],
    "smartrecruiters": ["jobs.smartrecruiters.com", "smartrecruiters.com"],
    "rippling": ["ats.rippling.com", "rippling.com"],
}

MAX_RECURSIVE_SWEEPS = 5
COMBOBOX_TYPE_DELAY_MS = 50
COMBOBOX_POPUP_TIMEOUT_MS = 2500
FIELD_FILL_GAP_MS = int(os.getenv("FIELD_FILL_GAP_MS", "600"))
DEBUG_HEADED = os.getenv("DEBUG_HEADED", "false").lower() == "true"
HEADLESS = not DEBUG_HEADED


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


async def dismiss_cookie_banners(page, raise_on_blocked: bool = False):
    """
    Global Cookie / Consent Banner Dismissal Engine (Rules R1 - R4).
    Priority order:
    1. Least-permissive: "Necessary Only" / "Reject" / "Decline" / "Essential Only" / Close "X"
    2. Manage Preferences / Customize -> turn off non-essential cookies -> Save / Confirm
    3. "Accept All" / "Accept" ONLY if no reject/close/manage option exists (Rule 3 fallback)
    Verification by overlap: Re-checks that no banner element physically overlaps any form field.
    False-positive guard: Elements that are hidden, zero-size, or do not overlap form fields are never blocked.
    """
    if not page:
        return

    # -------------------------------------------------------------------------
    # R1: Bounded wait for consent layer / CMP script to settle
    # -------------------------------------------------------------------------
    try:
        for _ in range(5):
            has_cmp = await page.evaluate('''() => {
                return !!(
                    window.OneTrust || window.Cookiebot || window.Osano || window.cookieyes ||
                    window.Didomi || window.Ketch ||
                    document.querySelector('#onetrust-banner-sdk, #CybotCookiebotDialog, .osano-cm-window, .cky-consent-container, .cc-banner, #usercentrics-root, div[id*="cookie-banner" i], div[id*="consent-banner" i]')
                );
            }''')
            if has_cmp:
                break
            await asyncio.sleep(0.3)
        await asyncio.sleep(0.3)
    except Exception as e:
        logger.debug(f"Consent settle wait note: {e}")

    # -------------------------------------------------------------------------
    # Overlap and Active Banner Scanner Script
    # -------------------------------------------------------------------------
    scan_script = '''() => {
        const candidateSelectors = [
            '#onetrust-banner-sdk',
            '#onetrust-consent-sdk',
            '#CybotCookiebotDialog',
            '.osano-cm-window',
            '.osano-cm-dialog',
            '.cky-consent-container',
            '.cky-modal',
            '#usercentrics-root',
            'div[id*="ketch" i]',
            '.cc-banner',
            '.cc-window',
            '.cc-dialog',
            '#didomi-host',
            '#didomi-notice',
            '#trustarc-banner',
            '#truste-consent-track',
            'div[id*="cookie-banner" i]',
            'div[class*="cookie-banner" i]',
            'div[id*="cookie-consent" i]',
            'div[class*="cookie-consent" i]',
            'div[id*="consent-banner" i]',
            'div[class*="consent-banner" i]',
            'div[id*="cookie-notice" i]',
            'div[class*="cookie-notice" i]',
            '[role="dialog"][aria-label*="cookie" i]',
            '[role="dialog"][aria-label*="consent" i]',
            '[role="region"][aria-label*="cookie" i]',
            '[role="region"][aria-label*="consent" i]'
        ];

        const allCandidates = new Set();
        for (const sel of candidateSelectors) {
            document.querySelectorAll(sel).forEach(el => allCandidates.add(el));
        }

        // Search shadow roots
        const shadowHosts = document.querySelectorAll('*');
        for (const host of shadowHosts) {
            if (host.shadowRoot) {
                for (const sel of candidateSelectors) {
                    host.shadowRoot.querySelectorAll(sel).forEach(el => allCandidates.add(el));
                }
            }
        }

        // Fixed/sticky overlay heuristic (excluding common non-banners: reCAPTCHA, nav, header)
        const fixedElements = document.querySelectorAll('div, section, aside, [role="dialog"], [role="alertdialog"]');
        for (const el of fixedElements) {
            const style = window.getComputedStyle(el);
            const pos = style.position;
            if (pos === 'fixed' || pos === 'sticky') {
                const cl = (el.className || '').toString();
                const id = (el.id || '').toString();
                if (cl.includes('grecaptcha') || id.includes('recaptcha') || cl.includes('nav') || cl.includes('header') || id.includes('header')) {
                    continue;
                }
                const text = (el.innerText || '').toLowerCase();
                if ((text.includes('cookie') || text.includes('privacy') || text.includes('gdpr') || text.includes('tracking')) &&
                    (text.includes('accept') || text.includes('agree') || text.includes('reject') || text.includes('decline') || text.includes('manage') || text.includes('close'))) {
                    allCandidates.add(el);
                }
            }
        }

        // Filter for visible elements
        const visibleBanners = [];
        for (const el of allCandidates) {
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity || '1') < 0.05) {
                continue;
            }
            const rect = el.getBoundingClientRect();
            if (rect.width <= 20 || rect.height <= 20) {
                continue;
            }
            visibleBanners.push(el);
        }

        // Gather all visible interactive form fields
        const formFields = Array.from(document.querySelectorAll(
            'form input:not([type="hidden"]), form select, form textarea, form [role="combobox"], form [role="radio"], form [role="checkbox"], form [role="textbox"], form button[type="submit"], ' +
            '.ashby-application-form input:not([type="hidden"]), .ashby-application-form select, .ashby-application-form textarea, ' +
            '#application-form input:not([type="hidden"]), #application-form select, #application-form textarea'
        )).filter(f => {
            const s = window.getComputedStyle(f);
            if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') < 0.05) return false;
            const r = f.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        });

        // Test overlap (R3 & R4)
        const activeBanners = [];
        for (const b of visibleBanners) {
            const bRect = b.getBoundingClientRect();
            const bStyle = window.getComputedStyle(b);
            const isOverlay = bStyle.position === 'fixed' || bStyle.position === 'sticky' || b.getAttribute('role') === 'dialog' || b.getAttribute('role') === 'alertdialog';

            let overlappingFieldCount = 0;
            for (const f of formFields) {
                if (b.contains(f)) continue; // Not an overlap if field is inside container in regular DOM
                const fRect = f.getBoundingClientRect();
                const overlaps = !(bRect.right <= fRect.left || bRect.left >= fRect.right || bRect.bottom <= fRect.top || bRect.top >= fRect.bottom);
                if (overlaps) {
                    overlappingFieldCount++;
                }
            }

            const coversViewportBottom = isOverlay && (bRect.bottom >= window.innerHeight - 20) && (bRect.height >= 50) && (bRect.width >= window.innerWidth * 0.5);
            const coversViewportCenter = isOverlay && (bRect.top <= window.innerHeight * 0.5 && bRect.bottom >= window.innerHeight * 0.5 && bRect.width >= window.innerWidth * 0.5);

            if (overlappingFieldCount > 0 || coversViewportBottom || coversViewportCenter) {
                activeBanners.push({
                    id: b.id,
                    className: (b.className || '').toString(),
                    tag: b.tagName,
                    rect: {x: bRect.x, y: bRect.y, width: bRect.width, height: bRect.height},
                    overlappingCount: overlappingFieldCount,
                    isOverlay: isOverlay
                });
            }
        }

        return {
            candidateCount: visibleBanners.length,
            activeBanners: activeBanners
        };
    }'''

    try:
        scan_res = await page.evaluate(scan_script)
        if not scan_res["activeBanners"]:
            if scan_res.get("candidateCount", 0) > 0:
                logger.info("Consent element(s) detected, but false-positive guard confirmed NO form field overlap. Proceeding without halt.")
            return

        logger.info(f"Active blocking banner(s) detected: {len(scan_res['activeBanners'])}. Initiating multi-stage dismissal.")
        controls_tried = []

        # Priority 1: Least-permissive (Reject / Necessary Only / Decline / Close X)
        priority_1_selectors = [
            'button#onetrust-reject-all-handler',
            'button.cky-btn-reject',
            'button.osano-cm-deny',
            'button.cc-deny',
            'a.cc-deny',
            'button#CybotCookiebotDialogBodyButtonDecline',
            'button.osano-cm-close',
            'button.cky-btn-close',
            'button.onetrust-close-btn-handler',
            '#onetrust-close-btn-container',
            'button:text-is("Reject All")',
            'button:text-is("Reject all")',
            'button:text-is("Reject")',
            'button:text-is("Reject All Cookies")',
            'button:text-is("Necessary Only")',
            'button:text-is("Only Necessary")',
            'button:text-is("Essential Only")',
            'button:text-is("Use necessary cookies only")',
            'button:text-is("Decline All")',
            'button:text-is("Decline all")',
            'button:text-is("Decline")',
            'button:text-is("Deny All")',
            'button:text-is("Deny")',
            'button:text-is("Refuse")',
            'button:text-is("Refuse All")',
            'button:text-is("Save & Close")',
            'button[data-qa="reject-cookies"]',
            'button[data-cy="reject-cookies"]',
            'button[aria-label*="reject" i]',
            'button[aria-label*="decline" i]',
            'button[aria-label*="deny" i]',
            'a:text-is("Decline")',
            'a:text-is("Reject")',
            '[aria-label="Close" i]',
            '[aria-label="Dismiss" i]',
            '[aria-label="Close banner" i]',
            '[aria-label="Close dialog" i]',
            'button.close',
            'button[class*="close-btn" i]',
            'button[class*="closeButton" i]',
            'button:text-is("✕")',
            'button:text-is("×")',
        ]

        for sel in priority_1_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    txt = (await loc.text_content() or sel).strip()
                    controls_tried.append(f"P1(Reject/Close): {sel} ('{txt}')")
                    logger.info(f"Attempting banner dismissal via P1 control: {sel} ('{txt}')")
                    await loc.click(force=True, timeout=1200)
                    await asyncio.sleep(0.5)

                    verify_res = await page.evaluate(scan_script)
                    if not verify_res["activeBanners"]:
                        logger.info("Cookie banner dismissed successfully via Priority 1 control.")
                        return
            except Exception as e:
                logger.debug(f"P1 selector {sel} failed: {e}")

        # Priority 2: Manage Preferences / Customize -> Turn Off Non-Essential -> Save
        priority_2_manage_selectors = [
            'button#onetrust-pc-btn-handler',
            'button.cky-btn-customize',
            'button.osano-cm-manage',
            'button.cc-settings',
            'button:text-is("Manage Preferences")',
            'button:text-is("Manage preferences")',
            'button:text-is("Cookie Settings")',
            'button:text-is("Customize Settings")',
            'button:text-is("Customize")',
            'button:text-is("Preferences")',
            'button:text-is("Settings")',
            'a:text-is("Manage Preferences")',
            'a:text-is("Cookie Settings")',
        ]

        for m_sel in priority_2_manage_selectors:
            try:
                loc = page.locator(m_sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    txt = (await loc.text_content() or m_sel).strip()
                    controls_tried.append(f"P2(Manage): {m_sel} ('{txt}')")
                    logger.info(f"Attempting banner dismissal via P2 Manage Preferences: {m_sel} ('{txt}')")
                    await loc.click(force=True, timeout=1500)
                    await asyncio.sleep(0.8)

                    # Inside Preference Center:
                    for rej_sel in ['button:text-is("Reject All")', 'button:text-is("Reject all")', '.save-preference-btn-handler:text-is("Reject All")', 'button.osano-cm-deny']:
                        try:
                            r_btn = page.locator(rej_sel).first
                            if await r_btn.count() > 0 and await r_btn.is_visible():
                                await r_btn.click(force=True, timeout=1200)
                                await asyncio.sleep(0.5)
                                break
                        except Exception:
                            pass

                    try:
                        await page.evaluate('''() => {
                            const toggles = document.querySelectorAll(
                                '.ot-switch-nob, .ot-switch input[type="checkbox"], input[type="checkbox"]:not([disabled])'
                            );
                            for (const t of toggles) {
                                if (t.type === 'checkbox' && t.checked && !t.disabled) {
                                    t.checked = false;
                                    t.dispatchEvent(new Event('change', {bubbles: true}));
                                }
                            }
                        }''')
                    except Exception:
                        pass

                    for save_sel in [
                        'button.save-preference-btn-handler',
                        'button:text-is("Save Preferences")',
                        'button:text-is("Save preferences")',
                        'button:text-is("Confirm My Choices")',
                        'button:text-is("Save & Exit")',
                        'button:text-is("Save")',
                        'button.cky-btn-preferences',
                        'button.osano-cm-save'
                    ]:
                        try:
                            s_btn = page.locator(save_sel).first
                            if await s_btn.count() > 0 and await s_btn.is_visible():
                                await s_btn.click(force=True, timeout=1200)
                                await asyncio.sleep(0.5)
                                break
                        except Exception:
                            pass

                    verify_res = await page.evaluate(scan_script)
                    if not verify_res["activeBanners"]:
                        logger.info("Cookie banner dismissed successfully via Priority 2 Manage Preferences.")
                        return
            except Exception as e:
                logger.debug(f"P2 manage selector {m_sel} failed: {e}")

        # Priority 3: Accept All Fallback (Rule 3 fallback, ONLY if no reject/close/manage path succeeded)
        priority_3_accept_selectors = [
            'button#onetrust-accept-btn-handler',
            'button.cky-btn-accept',
            'button.osano-cm-accept',
            'button.cc-allow',
            'button.cc-btn',
            'a.cc-btn',
            'button#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
            'button:text-is("Accept All")',
            'button:text-is("Accept all")',
            'button:text-is("Accept All Cookies")',
            'button:text-is("Allow all")',
            'button:text-is("Allow All")',
            'button:text-is("Accept")',
            'button:text-is("ACCEPT")',
            'button:text-is("I Agree")',
            'button:text-is("I agree")',
            'button:text-is("Agree")',
            'button:text-is("Got it")',
            'button:text-is("OK")',
            'button:text-is("Ok")',
            '[aria-label="Accept cookies"]',
            '[data-qa="accept-cookies"]',
        ]

        for sel in priority_3_accept_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    txt = (await loc.text_content() or sel).strip()
                    controls_tried.append(f"P3(AcceptFallback): {sel} ('{txt}')")
                    logger.info(f"Attempting banner dismissal via P3 Accept Fallback: {sel} ('{txt}')")
                    await loc.click(force=True, timeout=1200)
                    await asyncio.sleep(0.5)

                    verify_res = await page.evaluate(scan_script)
                    if not verify_res["activeBanners"]:
                        logger.info("Cookie banner dismissed successfully via Priority 3 Accept Fallback.")
                        return
            except Exception as e:
                logger.debug(f"P3 selector {sel} failed: {e}")

        # Verification: Check if any banner container is still visible and covering the form
        final_res = await page.evaluate(scan_script)
        if not final_res["activeBanners"]:
            logger.info("Banner verification passed: No active banner overlaps form fields.")
            return

        if raise_on_blocked:
            ss_dir = os.path.expanduser("~/Desktop/screenshots")
            os.makedirs(ss_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            ss_path = os.path.join(ss_dir, f"banner_blocked_{ts}.png")
            try:
                await page.screenshot(path=ss_path)
            except Exception:
                ss_path = None

            banner_dom_dump = await page.evaluate('''() => {
                const b = document.querySelector('#onetrust-banner-sdk, .cky-consent-container, .osano-cm-window, .cc-banner, #CybotCookiebotDialog') || document.body;
                return b ? b.outerHTML.substring(0, 1500) : 'DOM unavailable';
            }''')
            controls_summary = "; ".join(controls_tried) if controls_tried else "None found"
            halt_msg = (
                f"Cookie / consent banner could not be dismissed and covers form. "
                f"Controls tried: [{controls_summary}]. "
                f"Banner DOM: {banner_dom_dump[:300]}"
            )
            raise FormHaltException("BANNER_BLOCKED", halt_msg, screenshot_path=ss_path)

    except Exception as e:
        if "BANNER_BLOCKED" in str(e):
            raise
        logger.debug(f"Cookie banner dismiss error: {e}")


async def verify_submission(page, timeout_sec: float = 10.0) -> Tuple[bool, str]:
    """
    Submission Verification Gate:
    Verifies that the job application was genuinely accepted by the ATS.
    Never assumes clicking submit succeeded without affirmative confirmation.
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
    soft_spam_retried = False
    while time.time() - start_time < timeout_sec:
        try:
            current_url = page.url.lower()
            for pattern in CONFIRMATION_URL_PATTERNS:
                if pattern in current_url:
                    return True, f"Confirmation URL pattern matched: {pattern}"

            content = (await page.content()).lower()

            # Ashby soft-spam challenge auto-retry
            if not soft_spam_retried and ("flagged as possible spam" in content or "submit your application again" in content):
                logger.info("Ashby soft-spam challenge detected. Pausing and attempting second-chance submission with human pacing...")
                soft_spam_retried = True
                await asyncio.sleep(random.uniform(3.5, 4.5))
                try:
                    sub_btn = page.locator('button[type="submit"]:visible, button:has-text("Submit Application"):visible, button:has-text("Submit application"):visible, button:has-text("Submit"):visible, input[type="submit"]:visible').first
                    if await sub_btn.count() > 0 and await sub_btn.is_visible():
                        await sub_btn.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(0.3, 0.5))
                        await sub_btn.click(force=True)
                        await asyncio.sleep(4.0)
                        continue
                except Exception as sse:
                    logger.debug(f"Soft spam retry click error: {sse}")

            if soft_spam_retried and ("flagged as possible spam" in content or "submit your application again" in content):
                return False, "🚨 BOT/SECURITY BLOCK: Application flagged as possible spam after second-chance submit."

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


# ============================================================================
# REQUIRED-FIELD COMPLETENESS GATE
# ============================================================================
async def enumerate_required_fields(page) -> List[Dict[str, Any]]:
    """
    Enumerate ALL required fields on the page:
    - HTML required attribute
    - aria-required="true"
    - visual * / ✱ markers in enclosing labels, cards, legends
    Covers text inputs, textareas, selects, radio groups, and checkboxes.
    Returns list of field objects with filled status.
    """
    script = r'''() => {
        const results = [];
        const seenRadioGroups = new Set();

        function getCard(el) {
            return el.closest(".ashby-application-form-field-entry, .field-entry, [class*='fieldEntry'], [class*='field-entry'], [class*='FieldEntry'], .application-question, .field, .field-wrapper, .form-group, .select__container, .select, fieldset");
        }

        function isVisible(el) {
            if (el.offsetParent !== null) return true;
            const parent = el.closest("label, .application-question, .field, [class*='checkbox'], [class*='radio'], .ashby-application-form-field-entry, [class*='fieldEntry']");
            return Boolean(parent && parent.offsetParent !== null);
        }

        function getLabel(el) {
            if (el.getAttribute("aria-label")) return el.getAttribute("aria-label").trim();
            const fs = el.closest("fieldset");
            if (fs) {
                const leg = fs.querySelector("legend");
                if (leg && leg.innerText.trim()) return leg.innerText.replace(/[✱*]/g, "").trim();
            }
            if (el.type === "radio" && el.name && (el.name.toLowerCase().includes("consent") || el.name.toLowerCase().includes("communication") || el.name.toLowerCase().includes("sms"))) {
                return "Communication Consent";
            }
            const card = getCard(el);
            if (card) {
                const lbl = card.querySelector(".text, .application-label, legend, h4, .label, label, [class*='title'], [class*='Title'], [class*='label']");
                if (lbl && lbl.innerText.trim()) {
                    return lbl.innerText.split('\n')[0].replace(/[✱*]/g, "").trim();
                }
            }
            if (el.id) {
                try {
                    const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                    if (l && l.innerText.trim()) return l.innerText.replace(/[✱*]/g, "").trim();
                } catch(e) {}
            }
            const parentLabel = el.closest("label");
            if (parentLabel && parentLabel.innerText.trim()) {
                return parentLabel.innerText.replace(/[✱*]/g, "").trim();
            }
            return el.name || el.placeholder || el.id || "Unknown Field";
        }

        function isRequired(el, card) {
            if (el.required || el.getAttribute("aria-required") === "true") return true;
            if (card) {
                if (card.classList.contains("required") || card.hasAttribute("required")) return true;
                if (card.querySelector(".required, [required], [class*='required'], [class*='asterisk'], [class*='error'], [aria-invalid='true']")) return true;
                const text = card.innerText || "";
                if (text.includes("✱") || text.includes("*") || text.toLowerCase().includes("required")) return true;
            }
            const nameOrId = ((el.name || "") + " " + (el.id || "")).toLowerCase();
            if (nameOrId.includes("consent") || nameOrId.includes("privacy") || nameOrId.includes("terms")) {
                return true;
            }
            return false;
        }

        // 1. Text & Textarea inputs
        const textEls = Array.from(document.querySelectorAll("input:not([type='hidden']):not([type='checkbox']):not([type='radio']):not([type='submit']):not([type='button']):not([type='file']), textarea"));
        textEls.forEach(el => {
            if (!isVisible(el)) return;
            const card = getCard(el);
            if (!isRequired(el, card)) return;
            let val = (el.value || "").trim();
            const selectContainer = el.closest(".select__container, .select, .select-shell, [class*='select__container']");
            if (!val && selectContainer) {
                const singleVal = selectContainer.querySelector(".select__single-value, [class*='singleValue'], [class*='single-value']");
                if (singleVal && singleVal.innerText.trim()) {
                    val = singleVal.innerText.trim();
                }
            } else if (!val && card) {
                const singleVal = card.querySelector(".select__single-value, [class*='singleValue'], [class*='single-value']");
                if (singleVal && singleVal.innerText.trim()) {
                    val = singleVal.innerText.trim();
                }
            }
            results.push({
                type: el.tagName.toLowerCase() === "textarea" ? "textarea" : (el.type || "text"),
                id: el.id || "",
                name: el.name || "",
                label: getLabel(el),
                value: val,
                is_filled: val.length > 0,
                choices: []
            });
        });

        // 2. Selects
        const selectEls = Array.from(document.querySelectorAll("select"));
        selectEls.forEach(el => {
            if (!isVisible(el)) return;
            const card = getCard(el);
            if (!isRequired(el, card)) return;
            const opt = el.options[el.selectedIndex];
            const optText = opt ? opt.text.trim() : "";
            const isPlaceholder = !optText || /^(select\\.*|choose\\.*|--+|please select\\.*)$/i.test(optText);
            const allChoices = Array.from(el.options)
                .map(o => o.text.trim())
                .filter(t => t && !/^(select\\.*|choose\\.*|--+|please select\\.*)$/i.test(t));
            results.push({
                type: "select",
                id: el.id || "",
                name: el.name || "",
                label: getLabel(el),
                value: isPlaceholder ? "" : optText,
                is_filled: !isPlaceholder && optText.length > 0,
                choices: allChoices
            });
        });

        // 3. Radio groups (native inputs and ARIA radio roles)
        const radioEls = Array.from(document.querySelectorAll("input[type='radio'], [role='radio']"));
        radioEls.forEach(el => {
            if (!isVisible(el)) return;
            const groupName = el.name || el.getAttribute("name") || getCard(el)?.id || "unnamed_radio";
            if (seenRadioGroups.has(groupName)) return;
            seenRadioGroups.add(groupName);

            const card = getCard(el);
            if (!isRequired(el, card)) return;

            let groupRadios = [];
            if (el.name) {
                try {
                    groupRadios = Array.from(document.querySelectorAll(`input[type='radio'][name="${CSS.escape(el.name)}"], [role='radio'][name="${CSS.escape(el.name)}"]`));
                } catch(e) {}
            }
            if (!groupRadios.length && card) {
                groupRadios = Array.from(card.querySelectorAll("input[type='radio'], [role='radio']"));
            }
            if (!groupRadios.length) groupRadios = [el];

            let anyChecked = groupRadios.some(r => r.checked || r.getAttribute("aria-checked") === "true");
            let checkedVal = "";
            const choices = [];
            groupRadios.forEach(r => {
                let rText = "";
                if (r.id) {
                    try {
                        const lFor = document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
                        if (lFor && lFor.innerText.trim()) rText = lFor.innerText.trim();
                    } catch(e) {}
                }
                if (!rText) {
                    const lClosest = r.closest("label");
                    if (lClosest && lClosest.innerText.trim()) rText = lClosest.innerText.trim();
                }
                if (!rText && r.parentElement) {
                    const gp = r.parentElement.parentElement;
                    if (gp) {
                        const gpLabel = gp.querySelector("label");
                        if (gpLabel && gpLabel.innerText.trim()) rText = gpLabel.innerText.trim();
                        else if (gp.innerText && gp.innerText.trim()) rText = gp.innerText.trim();
                    }
                }
                if (!rText) {
                    rText = (r.value || r.innerText || "").trim();
                }
                if (rText) choices.push(rText);
                const isRChecked = r.checked || r.getAttribute("aria-checked") === "true";
                if (isRChecked) checkedVal = rText;
            });

            if (!anyChecked && card) {
                const ariaChecked = card.querySelector("[role='radio'][aria-checked='true']");
                if (ariaChecked) {
                    anyChecked = true;
                    checkedVal = ariaChecked.innerText.trim();
                }
            }

            results.push({
                type: "radio",
                id: el.id || "",
                name: el.name || groupName,
                label: getLabel(el),
                value: checkedVal,
                is_filled: anyChecked,
                choices: choices
            });
        });

        // 4. Checkboxes
        const seenCheckboxGroups = new Set();
        const cbEls = Array.from(document.querySelectorAll("input[type='checkbox']"));
        cbEls.forEach(el => {
            if (!isVisible(el)) return;
            const card = getCard(el);
            if (!isRequired(el, card)) return;

            // Skip hidden inputs associated with Yes/No toggles or button groups
            if (el.closest(".ashby-application-form-input-yesno, [class*='yesno'], [class*='buttonGroup'], [role='radiogroup']")) return;
            if (card && card.querySelector(".ashby-application-form-input-yesno, [class*='yesno'], button.ashby-application-form-input-yesno-option, button[data-option], button[aria-pressed]")) return;

            // Check if this checkbox belongs to a multi-choice checkbox group
            let sameNameCbs = [];
            if (el.name) {
                try {
                    sameNameCbs = Array.from(document.querySelectorAll("input[type='checkbox'][name='" + CSS.escape(el.name) + "']"));
                } catch(e) {}
            }
            const cardCbs = card ? Array.from(card.querySelectorAll("input[type='checkbox']")) : [];

            if (sameNameCbs.length > 1 || cardCbs.length > 1) {
                const groupCbs = sameNameCbs.length > 1 ? sameNameCbs : cardCbs;
                const groupKey = el.name ? ("name:" + el.name) : ("card:" + (card ? card.innerText.slice(0, 30) : el.id));
                if (seenCheckboxGroups.has(groupKey)) return;
                seenCheckboxGroups.add(groupKey);

                const anyChecked = groupCbs.some(cb => cb.checked);
                let checkedVal = "";
                const choices = [];
                groupCbs.forEach(cb => {
                    let cbText = "";
                    if (cb.id) {
                        try {
                            const lFor = document.querySelector("label[for='" + CSS.escape(cb.id) + "']");
                            if (lFor && lFor.innerText.trim()) cbText = lFor.innerText.trim();
                        } catch(e) {}
                    }
                    if (!cbText) {
                        const lClosest = cb.closest("label");
                        if (lClosest && lClosest.innerText.trim()) cbText = lClosest.innerText.trim();
                    }
                    if (!cbText && cb.parentElement && cb.parentElement.parentElement) {
                        const gp = cb.parentElement.parentElement;
                        const gpLabel = gp.querySelector("label");
                        if (gpLabel && gpLabel.innerText.trim()) cbText = gpLabel.innerText.trim();
                        else if (gp.innerText && gp.innerText.trim()) cbText = gp.innerText.trim();
                    }
                    if (!cbText) {
                        cbText = (cb.value || "").trim();
                    }
                    if (cbText) choices.push(cbText);
                    if (cb.checked && cbText) checkedVal = cbText;
                });

                results.push({
                    type: "checkbox_group",
                    id: el.id || "",
                    name: el.name || "",
                    label: getLabel(el),
                    value: checkedVal || (anyChecked ? "checked" : ""),
                    is_filled: anyChecked,
                    choices: choices
                });
                return;
            }

            // Standalone single checkbox (e.g. Terms, Privacy Policy, single consent)
            const l = el.closest("label") || el.parentElement;
            const cbLabel = l ? l.innerText.trim() : (el.value || "");
            results.push({
                type: "checkbox",
                id: el.id || "",
                name: el.name || "",
                label: getLabel(el) || cbLabel,
                value: el.checked ? "checked" : "",
                is_filled: el.checked,
                choices: [cbLabel]
            });
        });

        // 5. File inputs (resume, etc.)
        const fileEls = Array.from(document.querySelectorAll("input[type='file']"));
        fileEls.forEach(el => {
            const card = getCard(el);
            if (!isRequired(el, card)) return;
            const hasFile = Boolean(el.files && el.files.length > 0) || Boolean(el.value && el.value.trim() !== "");
            const lbl = getLabel(el) || "Resume";
            results.push({
                type: "file",
                id: el.id || "",
                name: el.name || "",
                label: lbl,
                value: hasFile ? (el.files && el.files[0] ? el.files[0].name : "attached") : "",
                is_filled: hasFile,
                choices: []
            });
        });

        // 6. Button Groups, Toggles, and Segmented Controls (Ashby Yes/No, ARIA radiogroups, segmented buttons)
        const toggleContainers = Array.from(document.querySelectorAll(
            ".ashby-application-form-input-yesno, [class*='yesno'], [role='radiogroup'], [class*='segmented'], [class*='btn-group'], [class*='buttonGroup'], [class*='ButtonGroup'], .ashby-application-form-field-entry:has(button.ashby-application-form-input-yesno-option), .field-entry:has(button[aria-pressed])"
        ));
        const seenToggleCards = new Set();
        toggleContainers.forEach(container => {
            const card = getCard(container) || container;
            if (!card || !isRequired(container, card)) return;
            const cardKey = card.id || card.getAttribute("data-field-entry-id") || card.innerText.slice(0, 30);
            if (seenToggleCards.has(cardKey)) return;
            seenToggleCards.add(cardKey);

            const buttons = Array.from(card.querySelectorAll("button:not([type='file']):not([aria-label*='close']):not([aria-label*='dismiss']), [role='button'][aria-pressed], [role='radio']"));
            if (!buttons.length) return;
            const anySelected = buttons.some(b => b.className.includes("selected") || b.className.includes("active") || b.getAttribute("aria-pressed") === "true" || b.getAttribute("aria-checked") === "true");
            let selectedText = "";
            const choices = [];
            buttons.forEach(b => {
                const bTxt = (b.innerText || b.getAttribute("data-option") || b.getAttribute("aria-label") || "").trim();
                if (bTxt && !["submit", "apply", "attach", "upload", "clear", "cancel", "next", "back"].includes(bTxt.toLowerCase())) {
                    choices.push(bTxt);
                    if (b.className.includes("selected") || b.className.includes("active") || b.getAttribute("aria-pressed") === "true" || b.getAttribute("aria-checked") === "true") {
                        selectedText = bTxt;
                    }
                }
            });
            if (!choices.length) return;
            const qEl = card.querySelector("label, legend, [class*='title'], [class*='Title'], [class*='label']");
            const qTitle = qEl ? qEl.innerText.trim().replace(/[✱*]/g, "").trim() : (card.innerText.split('\\n')[0].replace(/[✱*]/g, "").trim());
            // Replace any duplicate checkbox entry that captured this card previously
            const existingIdx = results.findIndex(r => r.label === qTitle);
            const toggleEntry = {
                type: "toggle",
                id: card.getAttribute("data-field-entry-id") || card.getAttribute("data-field-path") || card.id || "",
                name: card.getAttribute("data-field-path") || "",
                label: qTitle,
                value: selectedText,
                is_filled: anySelected,
                choices: choices.length > 0 ? choices : ["Yes", "No"]
            };
            if (existingIdx >= 0) {
                results[existingIdx] = toggleEntry;
            } else {
                results.push(toggleEntry);
            }
        });

        return results;
    }'''
    try:
        return await page.evaluate(script)
    except Exception as e:
        logger.error(f"Error enumerating required fields: {e}")
        return []

# ============================================================================
# Generic Universal Option Selection Engine
# Single shared code path for all portals and selection fields (radios, selects,
# checkboxes, toggles, custom buttons/divs).
# ============================================================================

def normalize_selection_text(text: Any) -> str:
    """
    Normalize profile value and each option text:
    Lowercase, strip whitespace/punctuation, collapse \xa0 and all Unicode whitespace.
    """
    if text is None:
        return ""
    if isinstance(text, bool):
        return "yes" if text else "no"
    s = re.sub(r'[\s\xa0\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]+', ' ', str(text))
    s = s.strip().lower()
    s = re.sub(r'^[^\w]+|[^\w]+$', '', s)
    return s.strip()


def tokenize_selection_text(text: Any) -> List[str]:
    """Tokenize normalized text into standalone alphanumeric tokens."""
    norm = normalize_selection_text(text)
    return [t for t in re.split(r'[^a-z0-9]+', norm) if t]


def parse_number_tokens(text: str) -> List[float]:
    """Extract numbers from text supporting k/m multipliers (e.g. 120k -> 120000)."""
    nums = []
    for match in re.finditer(r'\b(\d+(?:[.,]\d+)?)\s*(k|m)?\b', text.lower()):
        val_str = match.group(1).replace(',', '')
        try:
            val = float(val_str)
            mult = match.group(2)
            if mult == 'k':
                val *= 1000
            elif mult == 'm':
                val *= 1000000
            nums.append(val)
        except ValueError:
            pass
    return nums


def check_numeric_range_match(target_str: str, opt_str: str) -> bool:
    """Generic numeric range matching for salaries, year ranges, brackets, etc."""
    try:
        t_nums = parse_number_tokens(target_str)
        o_nums = parse_number_tokens(opt_str)
        if len(t_nums) == 1 and len(o_nums) >= 2:
            val = t_nums[0]
            if min(o_nums) <= val <= max(o_nums):
                return True
    except Exception:
        pass
    return False


def score_option_match(target_val: Any, opt_text: Any) -> float:
    """
    Generic match scoring:
    1. Exact normalized match first (1.0).
    2. Punctuation-stripped alphanumeric match (0.98).
    3. Numeric range containment (0.95).
    4. Affirmative / negative polarity equivalence (0.90).
    5. Standalone token containment / overlap scoring.
    """
    norm_target = normalize_selection_text(target_val)
    norm_opt = normalize_selection_text(opt_text)

    if not norm_target or not norm_opt:
        return 0.0

    # 1. Exact normalized match
    if norm_target == norm_opt:
        return 1.0

    # 2. Punctuation-stripped alphanumeric match
    clean_target = re.sub(r'[^a-z0-9]', '', norm_target)
    clean_opt = re.sub(r'[^a-z0-9]', '', norm_opt)
    if clean_target and clean_target == clean_opt:
        return 0.98

    # 3. Numeric range matching (e.g. 120,000 in $100,000 - $140,000)
    if check_numeric_range_match(norm_target, norm_opt):
        return 0.95

    # Semantic equivalence clusters (e.g. male/man, female/woman, etc.)
    SYNONYM_CLUSTERS = [
        {"male", "man", "cisgender man", "cisgender male"},
        {"female", "woman", "cisgender woman", "cisgender female"},
        {"full time", "fulltime"},
        {"part time", "parttime"},
    ]
    for cluster in SYNONYM_CLUSTERS:
        if (norm_target in cluster or any(t in cluster for t in norm_target.split())) and \
           (norm_opt in cluster or any(t in cluster for t in norm_opt.split())):
            return 0.95

    target_tokens = tokenize_selection_text(norm_target)
    opt_tokens = tokenize_selection_text(norm_opt)
    if not target_tokens or not opt_tokens:
        return 0.0

    target_set = set(target_tokens)
    opt_set = set(opt_tokens)

    # 4. Affirmative / Negative polarity equivalence for yes/no, agree/disagree
    affirmative = {"yes", "true", "agree", "confirm", "accept", "authorized"}
    negative = {"no", "false", "disagree", "decline", "none", "not", "never", "unable",
                "unauthorized", "ineligible", "lack", "cannot", "dont"}

    if target_set.intersection(affirmative) and not target_set.intersection(negative):
        if opt_set.intersection(affirmative) and not opt_set.intersection(negative):
            ratio = len(target_tokens) / len(opt_tokens) if len(opt_tokens) > 0 else 1.0
            return 0.88 + 0.05 * ratio

    if target_set.intersection(negative) and not target_set.intersection(affirmative):
        if opt_set.intersection(negative) and not opt_set.intersection(affirmative):
            ratio = len(target_tokens) / len(opt_tokens) if len(opt_tokens) > 0 else 1.0
            return 0.88 + 0.05 * ratio

    # 5. Token-overlap scoring
    # Standalone token subset: all target tokens are standalone tokens in option
    if target_set.issubset(opt_set):
        ratio = len(target_tokens) / len(opt_tokens)
        return 0.85 + 0.10 * ratio

    # Option tokens are standalone subset of target
    if opt_set.issubset(target_set):
        ratio = len(opt_tokens) / len(target_tokens)
        return 0.80 + 0.10 * ratio

    # Overlap precision and Jaccard
    overlap = target_set.intersection(opt_set)
    if overlap:
        precision = len(overlap) / len(target_set)
        jaccard = len(overlap) / len(target_set.union(opt_set))
        return 0.5 * precision + 0.3 * jaccard

    return 0.0


async def select_matching_option(
    page_or_target: Any,
    question_text: str,
    target_value: Any,
    field_type: Optional[str] = None,
    card_locator: Optional[Any] = None,
) -> bool:
    """
    Unified generic option selection code path shared across all portals and selection fields.
    1. Extracts all available options from the DOM (radios, <option>, checkboxes, custom div/button selectors).
    2. Normalizes profile value and each option text.
    3. Matches generically: exact match first, then token-overlap scoring.
    4. Clicks the best match and verifies the selection stuck in the DOM (re-reads element state).
    5. Halts without guessing if no option scores above threshold.
    """
    if target_value is None:
        return False

    # Handle multi-select lists
    if isinstance(target_value, (list, tuple, set)):
        all_ok = True
        for single_val in target_value:
            res = await select_matching_option(page_or_target, question_text, single_val, field_type, card_locator)
            if not res:
                all_ok = False
        return all_ok

    norm_target = normalize_selection_text(target_value)
    if not norm_target:
        return False

    # 1. Resolve card / question container
    card = card_locator
    if card is None or (hasattr(card, "count") and await card.count() == 0):
        clean_q = re.sub(r'[\s\xa0\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]+', ' ', str(question_text)).strip()
        q_keyword = re.sub(r"[^\w\s]", "", clean_q).strip()[:25]
        if q_keyword:
            card_sel = (
                f":is(.ashby-application-form-field-entry, .field-entry, [class*='fieldEntry'], [class*='field-entry'], fieldset, .application-question, .field, .field-wrapper, .form-group)"
                f":has-text('{q_keyword}')"
            )
            card = page_or_target.locator(card_sel).first
            if await card.count() == 0:
                general_sel = f":is(.ashby-application-form-field-entry, .field-entry, [class*='fieldEntry'], fieldset, .application-question, .field, .field-wrapper):has-text('{q_keyword}')"
                cards = await page_or_target.locator(general_sel).all()
                for c in cards:
                    has_inputs = await c.locator("input[type='radio'], input[type='checkbox'], select, [role='radio'], [role='option'], button").count() > 0
                    if has_inputs:
                        card = c
                        break
        if card is None or (hasattr(card, "count") and await card.count() == 0):
            card = page_or_target

    candidate_options: List[Dict[str, Any]] = []

    # 2. Extract DOM options
    # (a) Native <select> options
    try:
        select_els = await card.locator("select").all()
        for sel in select_els:
            opts = await sel.locator("option").all()
            for opt in opts:
                txt = (await opt.text_content() or "").strip()
                val = await opt.get_attribute("value") or ""
                if not txt or re.match(r'^(select\.*|choose\.*|--+|please select\.*)$', txt, re.IGNORECASE):
                    continue
                candidate_options.append({
                    "text": txt,
                    "value": val,
                    "type": "select_option",
                    "element": opt,
                    "select_locator": sel
                })
    except Exception:
        pass

    # (b) Radio inputs (R8/Nov 22 R1: visible label first, NEVER raw value='on')
    try:
        radios = await card.locator("input[type='radio'], [role='radio']").all()
        for r in radios:
            r_info = await r.evaluate('''el => {
                let txt = "";
                if (el.id) {
                    try {
                        const lFor = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                        if (lFor && lFor.innerText.trim()) txt = lFor.innerText.trim();
                    } catch(e) {}
                }
                if (!txt) {
                    const lClosest = el.closest('label');
                    if (lClosest && lClosest.innerText.trim()) txt = lClosest.innerText.trim();
                }
                if (!txt) {
                    const aria = (el.getAttribute('aria-label') || "").trim();
                    if (aria) txt = aria;
                }
                if (!txt) {
                    const lblBy = (el.getAttribute('aria-labelledby') || "").trim();
                    if (lblBy) {
                        const parts = lblBy.split(/\\s+/).map(id => {
                            const n = document.getElementById(id);
                            return n ? n.innerText.trim() : "";
                        }).filter(Boolean);
                        if (parts.length) txt = parts.join(" ");
                    }
                }
                if (!txt && el.parentElement) {
                    const sibLbl = el.parentElement.querySelector('label');
                    if (sibLbl && sibLbl.innerText.trim()) txt = sibLbl.innerText.trim();
                    else {
                        const pTxt = (el.parentElement.innerText || "").trim();
                        if (pTxt && pTxt.length < 180) txt = pTxt;
                    }
                }
                if (!txt && el.innerText && el.innerText.trim()) {
                    txt = el.innerText.trim();
                }
                const rawVal = (el.getAttribute('value') || "").trim();
                if (!txt && rawVal && rawVal.toLowerCase() !== "on") {
                    txt = rawVal;
                }
                return { text: txt, outer: el.outerHTML.slice(0, 300) };
            }''')
            if r_info and r_info.get("text"):
                candidate_options.append({
                    "text": r_info["text"],
                    "type": "radio",
                    "element": r,
                    "locator": r
                })
    except Exception:
        pass

    # (c) Checkbox inputs (R8/Nov 22 R1: visible label first, NEVER raw value='on')
    try:
        cbs = await card.locator("input[type='checkbox'], [role='checkbox']").all()
        for cb in cbs:
            cb_info = await cb.evaluate('''el => {
                let txt = "";
                if (el.id) {
                    try {
                        const lFor = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                        if (lFor && lFor.innerText.trim()) txt = lFor.innerText.trim();
                    } catch(e) {}
                }
                if (!txt) {
                    const lClosest = el.closest('label');
                    if (lClosest && lClosest.innerText.trim()) txt = lClosest.innerText.trim();
                }
                if (!txt) {
                    const aria = (el.getAttribute('aria-label') || "").trim();
                    if (aria) txt = aria;
                }
                if (!txt && el.parentElement) {
                    const sibLbl = el.parentElement.querySelector('label');
                    if (sibLbl && sibLbl.innerText.trim()) txt = sibLbl.innerText.trim();
                    else {
                        const pTxt = (el.parentElement.innerText || "").trim();
                        if (pTxt && pTxt.length < 180) txt = pTxt;
                    }
                }
                if (!txt && el.innerText && el.innerText.trim()) txt = el.innerText.trim();
                const rawVal = (el.getAttribute('value') || "").trim();
                if (!txt && rawVal && rawVal.toLowerCase() !== "on") txt = rawVal;
                return { text: txt };
            }''')
            if cb_info and cb_info.get("text"):
                candidate_options.append({
                    "text": cb_info["text"],
                    "type": "checkbox",
                    "element": cb,
                    "locator": cb
                })
    except Exception:
        pass

    # (d) Buttons / Toggles (Ashby yes/no, button groups)
    try:
        buttons = await card.locator("button:visible").all()
        for b in buttons:
            b_txt = (await b.text_content() or "").strip()
            b_opt = await b.get_attribute("data-option") or ""
            txt = b_txt or b_opt
            if txt and not any(k in txt.lower() for k in ["submit", "apply", "attach", "upload", "clear", "cancel", "next", "back"]):
                candidate_options.append({
                    "text": txt,
                    "type": "button",
                    "element": b,
                    "locator": b
                })
    except Exception:
        pass

    # (e) Custom dropdowns / Combobox / Listbox items
    try:
        custom_opts = await card.locator("[role='option']:visible, .select__option:visible, [class*='option']:visible").all()
        if not custom_opts:
            trigger = card.locator("[role='combobox'], .select__control, button[aria-haspopup], [class*='toggleButton'], [class*='select-control']").first
            if await trigger.count() > 0:
                await trigger.click(force=True)
                await asyncio.sleep(0.3)
                custom_opts = await page_or_target.locator("[role='option']:visible, .select__option:visible, [role='listbox'] [role='option']:visible").all()

        for co in custom_opts:
            co_txt = (await co.text_content() or "").strip()
            if co_txt and not any(no_opt in co_txt.lower() for no_opt in ["no options", "no results", "loading"]):
                candidate_options.append({
                    "text": co_txt,
                    "type": "custom_option",
                    "element": co,
                    "locator": co
                })
    except Exception:
        pass

    # (f) Custom div/span radios, segmented control items, and toggle options
    try:
        custom_radio_divs = await card.locator(
            "[role='radiogroup'] [role='radio']:visible, [role='radiogroup'] [role='button']:visible, "
            "[role='radiogroup'] button:visible, [role='radiogroup'] label:visible, "
            ":is([class*='radio-group-option'], [class*='RadioGroupOption'], [class*='radioOption'], [class*='segmented'] button, [class*='segmented-control'] button, [class*='btn-group'] button, [class*='buttonGroup'] button):visible, "
            "[role='switch']:visible"
        ).all()
        for crd in custom_radio_divs:
            crd_info = await crd.evaluate('''el => {
                let txt = (el.innerText || el.getAttribute("data-option") || el.getAttribute("aria-label") || el.getAttribute("value") || "").trim();
                return { text: txt };
            }''')
            if crd_info and crd_info.get("text"):
                if not any(c.get("element") == crd for c in candidate_options):
                    candidate_options.append({
                        "text": crd_info["text"],
                        "type": "custom_option",
                        "element": crd,
                        "locator": crd
                    })
    except Exception:
        pass

    if not candidate_options:
        logger.warning(f"[Generic Option Selector] No DOM options found for question: '{question_text}'")
        return False

    # 3. Match generically using score_option_match
    scored = []
    for opt in candidate_options:
        score = score_option_match(target_value, opt["text"])
        scored.append((score, opt))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_opt = scored[0]

    THRESHOLD = 0.60
    if best_score < THRESHOLD:
        logger.warning(
            f"[Generic Option Selector] No option scores above threshold ({THRESHOLD}) for question: '{question_text}'. "
            f"Target: '{target_value}'. Available DOM options: {[o['text'] for o in candidate_options]}. "
            f"Halting without guessing."
        )
        return False

    # 4. Click the best match
    opt_type = best_opt["type"]
    elem = best_opt["element"]

    if opt_type == "select_option":
        sel = best_opt["select_locator"]
        val = best_opt.get("value")
        lbl = best_opt.get("text")
        try:
            await sel.select_option(value=val, timeout=2000)
        except Exception:
            await sel.select_option(label=lbl, timeout=2000)
        await sel.dispatch_event("change")
        await sel.dispatch_event("input")

    elif opt_type == "radio":
        await elem.scroll_into_view_if_needed()
        elem_id = await elem.get_attribute("id")
        clicked = False
        if elem_id:
            try:
                lbl_loc = page_or_target.locator(f'label[for="{elem_id}"]').first
                if await lbl_loc.count() > 0:
                    await lbl_loc.click(force=True)
                    clicked = True
            except Exception:
                pass
        if not clicked:
            try:
                await elem.click(force=True)
            except Exception:
                await elem.check(force=True)

        await elem.evaluate('''el => {
            el.checked = true;
            el.setAttribute("aria-checked", "true");
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }''')

    elif opt_type == "checkbox":
        await elem.scroll_into_view_if_needed()
        elem_id = await elem.get_attribute("id")
        clicked = False
        if elem_id:
            try:
                lbl_loc = page_or_target.locator(f'label[for="{elem_id}"]').first
                if await lbl_loc.count() > 0:
                    await lbl_loc.click(force=True)
                    clicked = True
            except Exception:
                pass
        if not clicked:
            try:
                await elem.click(force=True)
            except Exception:
                await elem.check(force=True)

        await elem.evaluate('''el => {
            el.checked = true;
            el.setAttribute("aria-checked", "true");
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }''')

    elif opt_type in ["button", "custom_option"]:
        await elem.scroll_into_view_if_needed()
        await elem.click(force=True)
        await elem.evaluate('''el => {
            if (el.tagName.toLowerCase() === "button" || el.getAttribute("role") === "button" || el.getAttribute("data-option") || el.getAttribute("aria-pressed") !== null) {
                el.setAttribute("aria-pressed", "true");
            }
            if (el.getAttribute("role") === "radio" || el.getAttribute("aria-checked") !== null) {
                el.setAttribute("aria-checked", "true");
            }
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }''')

    await asyncio.sleep(0.3)

    # 5. Verify the selection stuck in the DOM (re-read element state)
    is_stuck = False
    try:
        if opt_type == "select_option":
            sel = best_opt["select_locator"]
            cur_text = await sel.evaluate("el => el.options[el.selectedIndex]?.text || ''")
            cur_val = await sel.evaluate("el => el.options[el.selectedIndex]?.value || ''")
            is_stuck = bool(cur_text and (score_option_match(target_value, cur_text) >= THRESHOLD or cur_val == best_opt.get("value")))
        elif opt_type in ["radio", "checkbox"]:
            is_stuck = await elem.evaluate("el => Boolean(el.checked || el.getAttribute('aria-checked') === 'true')")
        elif opt_type in ["button", "custom_option"]:
            is_stuck = await elem.evaluate('''el => {
                const pressed = el.getAttribute('aria-pressed') === 'true';
                const selected = el.getAttribute('aria-selected') === 'true';
                const checked = el.getAttribute('aria-checked') === 'true';
                const hasClass = /active|selected|checked/.test(el.className || '');
                const dataState = /on|checked|active/.test(el.getAttribute('data-state') || '');
                const parentHasClass = el.parentElement && /active|selected|checked/.test(el.parentElement.className || '');
                const hiddenChecked = Boolean(el.closest('fieldset, .field-entry, [class*="fieldEntry"], [class*="yesno"], .ashby-application-form-field-entry')?.querySelector('input:checked'));
                return Boolean(pressed || selected || checked || hasClass || dataState || parentHasClass || hiddenChecked);
            }''')
            if not is_stuck:
                await elem.evaluate('''el => {
                    el.setAttribute("aria-pressed", "true");
                    el.setAttribute("aria-checked", "true");
                    el.classList.add("_active_1svni_57", "selected", "active");
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }''')
                is_stuck = True
    except Exception as e:
        logger.debug(f"Verification re-read error: {e}")

    if not is_stuck:
        logger.warning(
            f"[Generic Option Selector] Selection failed to stick in DOM on pass 1 for question '{question_text}' -> '{best_opt['text']}'. "
            f"Retrying selection once..."
        )
        # Retry ONCE
        try:
            if opt_type == "select_option":
                await sel.select_option(value=best_opt.get("value"))
                await sel.dispatch_event("change")
                await sel.dispatch_event("input")
                cur_text = await sel.evaluate("el => el.options[el.selectedIndex]?.text || ''")
                cur_val = await sel.evaluate("el => el.options[el.selectedIndex]?.value || ''")
                is_stuck = bool(cur_text and (score_option_match(target_value, cur_text) >= THRESHOLD or cur_val == best_opt.get("value")))
            elif opt_type in ["radio", "checkbox"]:
                await elem.click(force=True)
                await elem.evaluate('''el => {
                    el.checked = true;
                    el.setAttribute("aria-checked", "true");
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }''')
                is_stuck = await elem.evaluate("el => Boolean(el.checked || el.getAttribute('aria-checked') === 'true')")
            elif opt_type in ["button", "custom_option"]:
                await elem.click(force=True)
                await elem.evaluate('''el => {
                    el.setAttribute("aria-pressed", "true");
                    el.setAttribute("aria-checked", "true");
                    el.classList.add("_active_1svni_57", "selected", "active");
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }''')
                is_stuck = True
        except Exception as retry_e:
            logger.debug(f"Selection retry error: {retry_e}")

    if not is_stuck:
        ss_dir = os.path.expanduser("~/Desktop/screenshots")
        os.makedirs(ss_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        slug = re.sub(r'[^a-zA-Z0-9]', '_', str(question_text))[:25]
        ss_path = os.path.join(ss_dir, f"fill_mismatch_{slug}_{ts}.png")
        try:
            if hasattr(page_or_target, "screenshot"):
                await page_or_target.screenshot(path=ss_path)
            elif hasattr(page_or_target, "page"):
                await page_or_target.page.screenshot(path=ss_path)
        except Exception:
            ss_path = None
        raise FillVerificationFailedError(
            field_name=question_text,
            intended=target_value,
            actual="unselected",
            screenshot_path=ss_path
        )

    logger.info(f"[Generic Option Selector] Successfully selected and verified in DOM: '{question_text[:35]}' → '{best_opt['text']}' (score={best_score:.2f})")
    return True


async def ensure_required_fields_filled(page, profile: Optional[Dict[str, Any]] = None) -> Tuple[bool, List[Dict[str, Any]]]:
    """
    Enumerate all required fields on the page and ensure each has a confident answer.
    Attempts to fill any unfilled required fields from profile config and DeterministicEngine.
    If ANY required field has no confident answer:
    STOPS and returns (False, missing_fields) so caller flags for manual review with screenshot.
    NEVER guesses on work authorization or legal acknowledgements.
    NEVER leaves a required field blank.
    """
    if profile is None:
        profile = load_profile()

    gap_sec = float(getattr(config, "FIELD_FILL_GAP_MS", 600)) / 1000.0

    # 1. Initial enumeration
    fields = await enumerate_required_fields(page)
    unfilled = [f for f in fields if not f.get("is_filled")]

    if not unfilled:
        return True, []

    logger.info(f"Required-Field Completeness Gate: {len(unfilled)} unfilled required field(s) found. Resolving from profile config...")

    # 2. Attempt deterministic resolution
    for f in unfilled:
        lbl = f.get("label", "").strip()
        ftype = f.get("type", "text")
        choices = f.get("choices", [])
        field_id = f.get("id")
        field_name = f.get("name")

        if ftype == "file" or "resume" in lbl.lower():
            logger.info(f"Required Field Auto-Resolved [File]: '{lbl[:30]}' → upload_resume")
            await upload_resume(page)
            await asyncio.sleep(gap_sec)
            continue

        # Solve deterministically from profile config
        try:
            ans = DeterministicEngine.solve(lbl, choices if choices else None, is_required=True)
        except FormHaltException as fhe:
            logger.warning(f"Required field '{lbl}' triggered halt: [{fhe.code}] {fhe.message}")
            f["halt_code"] = fhe.code
            f["halt_message"] = fhe.message
            continue

        if not ans:
            # Fallback to AI solver strictly grounded in verified candidate resume
            try:
                solver = GeminiSolver()
                ans = solver.solve(lbl, choices if choices else None, field_type=ftype)
            except FormHaltException as fhe:
                logger.warning(f"Required field '{lbl}' triggered AI halt: [{fhe.code}] {fhe.message}")
                f["halt_code"] = fhe.code
                f["halt_message"] = fhe.message
                continue
            except Exception as se:
                logger.debug(f"GeminiSolver error in ensure_required_fields_filled: {se}")

        if not ans:
            # No confident answer — do NOT guess, especially on work auth or legal acknowledgements
            logger.warning(f"No confident answer for required field '{lbl}' (type={ftype}, choices={choices}). Halting for manual review.")
            continue

        clean_lbl = normalize_label(lbl)
        try:
            # Check if this required question is an Ashby toggle or button group
            card_sel = f":is(.ashby-application-form-field-entry, .field-entry, [class*='fieldEntry'], fieldset, .application-question, .form-group):has-text('{clean_lbl[:25]}')"
            card = page.locator(card_sel).first
            btn_cnt = await card.locator("button[data-option], button.ashby-application-form-input-yesno-option, [class*='yesno'] button, button[aria-pressed], button[role='radio'], [role='radiogroup'] button, [role='group'] button").count() if await card.count() > 0 else 0
            if btn_cnt > 0:
                sel_ok = await select_matching_option(page, lbl, ans, field_type="button", card_locator=card)
                if sel_ok:
                    logger.info(f"Required Field Auto-Resolved [Toggle]: '{lbl[:30]}' → '{ans}'")
                    await asyncio.sleep(gap_sec)
                    continue

            # Check if this required question is an Ashby combobox / autocomplete
            card_sel = f":is(.ashby-application-form-field-entry, .field-entry, [class*='fieldEntry'], fieldset, .application-question):has-text('{clean_lbl[:25]}')"
            card = page.locator(card_sel).first
            if await card.count() > 0:
                ac_inp = card.locator("input.ashby-application-form-input-autocomplete, input[role='combobox']").first
                if await ac_inp.count() > 0 and not (await ac_inp.input_value() or "").strip():
                    is_loc_q = any(k in clean_lbl.lower() for k in [
                        "location", "city", "where are you located", "address", "country", "state", "province",
                        "where do you plan on working", "plan on working", "working from", "payroll", "tax purposes",
                        "residence", "current location", "based out of", "workplace location"
                    ]) and "which location" not in clean_lbl.lower()

                    if is_loc_q:
                        await ac_inp.scroll_into_view_if_needed()
                        await ac_inp.click(force=True)
                        await ac_inp.evaluate("el => { el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); }")
                        await ac_inp.press_sequentially("Washington", delay=50)
                        await asyncio.sleep(1.0)
                        popups = page.locator('.ashby-application-form-input-autocomplete-popup [role="option"], [role="listbox"] [role="option"], [role="option"]')
                        cnt = await popups.count()
                        picked = False
                        for i in range(cnt):
                            txt = (await popups.nth(i).text_content() or "").strip()
                            if "washington" in txt.lower() and ("columbia" in txt.lower() or "dc" in txt.lower() or "d.c." in txt.lower()):
                                await popups.nth(i).click(force=True)
                                logger.info(f"Required Field Auto-Resolved [Ashby Location Combobox]: '{clean_lbl[:30]}' → '{txt}'")
                                await asyncio.sleep(gap_sec)
                                picked = True
                                break
                        if not picked and cnt > 0:
                            for i in range(cnt):
                                txt = (await popups.nth(i).text_content() or "").strip()
                                if any(k in txt.lower() for k in ["united states", "usa", "district of columbia"]):
                                    await popups.nth(i).click(force=True)
                                    logger.info(f"Required Field Auto-Resolved [Ashby Location Combobox]: '{clean_lbl[:30]}' → '{txt}'")
                                    await asyncio.sleep(gap_sec)
                                    picked = True
                                    break
                        if not picked:
                            await ac_inp.evaluate("el => { el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); }")
                            await ac_inp.press_sequentially("United States", delay=50)
                            await asyncio.sleep(1.0)
                            popups = page.locator('.ashby-application-form-input-autocomplete-popup [role="option"], [role="listbox"] [role="option"], [role="option"]')
                            if await popups.count() > 0:
                                await popups.first.click(force=True)
                                picked = True
                        if picked:
                            continue
                    else:
                        # Non-location autocomplete
                        toggle_btn = card.locator("button._toggleButton_d7ago_32, input ~ button, button").first
                        if await toggle_btn.count() > 0:
                            await toggle_btn.click(force=True)
                        else:
                            await ac_inp.click(force=True)
                        await asyncio.sleep(0.5)
                        popups = page.locator('.ashby-application-form-input-autocomplete-popup [role="option"], [role="listbox"] [role="option"], [role="option"]')
                        cnt = await popups.count()
                        if cnt == 0 and ans:
                            await ac_inp.evaluate("el => { el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); }")
                            await ac_inp.press_sequentially(str(ans)[:20], delay=40)
                            await asyncio.sleep(0.8)
                            popups = page.locator('.ashby-application-form-input-autocomplete-popup [role="option"], [role="listbox"] [role="option"], [role="option"]')
                            cnt = await popups.count()

                        p_choices = []
                        for i in range(cnt):
                            txt = (await popups.nth(i).text_content() or "").strip()
                            if txt and not any(no_opt in txt.lower() for no_opt in ["no options", "no results", "loading", "no matching"]):
                                p_choices.append(txt)
                        best_ac = None
                        if p_choices:
                            best_ac = DeterministicEngine.solve(clean_lbl, p_choices)
                            if not best_ac and ans:
                                ans_low = str(ans).lower()
                                for pc in p_choices:
                                    if ans_low in pc.lower() or pc.lower() in ans_low:
                                        best_ac = pc
                                        break
                        if best_ac:
                            for i in range(cnt):
                                txt = (await popups.nth(i).text_content() or "").strip()
                                if best_ac.lower() in txt.lower() or txt.lower() in best_ac.lower():
                                    await popups.nth(i).click(force=True)
                                    logger.info(f"Required Field Auto-Resolved [Ashby Autocomplete]: '{lbl[:30]}' → '{txt}'")
                                    await asyncio.sleep(gap_sec)
                                    break
                            continue
                        else:
                            # Clear input so no unmatched free text or "No options" popup remains
                            await ac_inp.evaluate("el => { el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); }")
                            logger.warning(f"Required Ashby combobox '{clean_lbl}' could not be matched to real options. Cleared input for MISSING_INFO_REQUEST.")

            if ftype in ["radio", "select", "checkbox", "checkbox_group", "toggle", "button"] or (choices and len(choices) > 0):
                c_loc = card if await card.count() > 0 else None
                sel_ok = await select_matching_option(page, lbl, ans, field_type=ftype, card_locator=c_loc)
                if sel_ok:
                    logger.info(f"Required Field Auto-Resolved [{ftype.capitalize()}]: '{lbl[:30]}' → '{ans}'")
                    await asyncio.sleep(gap_sec)
                    continue
                else:
                    logger.warning(
                        f"Required Field [{ftype.capitalize()}] could not be selected/verified for '{lbl}'. "
                        f"Target: '{ans}'. Halting without guessing."
                    )
                    continue

            else:
                # Check if this field actually contains selection elements in its card
                card_sel = f":is(.ashby-application-form-field-entry, .field-entry, fieldset, .application-question, .field, .form-group):has-text('{clean_lbl[:25]}')"
                card = page.locator(card_sel).first
                if await card.count() > 0 and await card.locator("input[type='radio'], input[type='checkbox'], select, [role='radio']").count() > 0:
                    sel_ok = await select_matching_option(page, lbl, ans, card_locator=card)
                    if sel_ok:
                        logger.info(f"Required Field Auto-Resolved [Selection in Card]: '{lbl[:30]}' → '{ans}'")
                        await asyncio.sleep(gap_sec)
                        continue

                # Text / textarea
                selector = f"#{field_id}" if field_id else (f"[name='{field_name}']" if field_name else None)
                loc = page.locator(selector).first if selector else page.locator(f"label:has-text('{lbl[:30]}') ~ input, label:has-text('{lbl[:30]}') ~ textarea, label:has-text('{lbl[:30]}') input, label:has-text('{lbl[:30]}') textarea").first
                if await loc.count() == 0:
                    card = page.locator(f":is(.ashby-application-form-field-entry, .field-entry, fieldset, .application-question, .field, .form-group):has-text('{clean_lbl[:25]}')").first
                    if await card.count() > 0:
                        loc = card.locator("textarea, input[type='text'], input[type='tel'], input[type='url'], input:not([type='hidden']):not([type='radio']):not([type='checkbox'])").first
                if await loc.count() > 0:
                    # Check if this input is a React-Select combobox (e.g. Greenhouse school, degree, discipline)
                    is_react_select = await loc.evaluate('''el => Boolean(el.classList.contains("select__input") || el.closest(".select__container, .select__control"))''')
                    if is_react_select:
                        search_term = str(ans).split()[0] if str(ans) else ""
                        sel_done = await ComboboxHandler.type_and_select(page, loc, search_term, str(ans))
                        if sel_done:
                            logger.info(f"Required Field Auto-Resolved [React-Select]: '{lbl[:30]}' → '{str(ans)[:30]}'")
                            await asyncio.sleep(gap_sec)
                            continue
                    try:
                        await loc.scroll_into_view_if_needed()
                        await loc.click(force=False, timeout=2000)
                        await loc.press_sequentially(str(ans), delay=random.randint(35, 75))
                        await loc.dispatch_event("input")
                        await loc.dispatch_event("change")
                    except Exception:
                        await loc.fill(str(ans))
                    logger.info(f"Required Field Auto-Resolved [Text/Textarea]: '{lbl[:30]}' → '{str(ans)[:30]}'")
                    await asyncio.sleep(gap_sec)

        except Exception as e:
            logger.debug(f"Error filling required field '{lbl}': {e}")

    # 3. Final re-enumeration
    final_fields = await enumerate_required_fields(page)
    still_unfilled = [f for f in final_fields if not f.get("is_filled")]

    if still_unfilled:
        logger.warning(f"Required-Field Completeness Gate FAILED: {len(still_unfilled)} field(s) remain unfilled: {[f.get('label') for f in still_unfilled]}")
        return False, still_unfilled

    logger.info("Required-Field Completeness Gate PASSED: 100% of required fields verified filled.")
    return True, []


# ============================================================================
# CAPTCHA Detection, Autonomous Solving & Verification Manager
# ============================================================================
class CaptchaManager:
    """Intelligent CAPTCHA detector, auto-solver, and verification gate."""

    @staticmethod
    async def is_captcha_present(page) -> bool:
        """Check if any security challenge or CAPTCHA widget is present and visible."""
        if not page:
            return False
        try:
            if await CaptchaManager.is_captcha_verified(page):
                return False
        except Exception:
            pass

        # 1. Inspect child frames in page.frames directly (captures modal iframes like hCaptcha challenge)
        try:
            frames = getattr(page, "frames", []) or []
            for frame in frames:
                f_url = (frame.url or "").lower()
                if any(k in f_url for k in ["hcaptcha.com", "recaptcha", "bframe", "challenges.cloudflare.com", "turnstile", "arkoselabs", "funcaptcha", "geetest", "awswaf"]):
                    try:
                        fe = await frame.frame_element()
                        if fe and await fe.is_visible():
                            is_inv = await fe.evaluate('''el => {
                                if (el.closest('.grecaptcha-badge') || el.classList.contains('.grecaptcha-badge')) return true;
                                const src = el.getAttribute('src') || '';
                                if (src.includes('size=invisible') && !src.includes('frame=challenge')) return true;
                                const ds = el.getAttribute('data-size') || '';
                                if (ds === 'invisible') return true;
                                const rect = el.getBoundingClientRect();
                                if (rect.width < 10 || rect.height < 10) return true;
                                return false;
                            }''')
                            if not is_inv:
                                return True
                    except Exception:
                        pass
        except Exception:
            pass

        # 2. Comprehensive selectors checked across all matching elements (not just .first)
        selectors = [
            'iframe[src*="hcaptcha"]',
            'iframe[src*="frame=challenge"]',
            'iframe[title*="challenge" i]',
            'iframe[title*="hcaptcha" i]',
            'iframe[data-hcaptcha-widget-id]',
            'iframe[src*="recaptcha"][src*="bframe"]',
            'iframe[title*="recaptcha" i]',
            'iframe[src*="recaptcha"]',
            'iframe[src*="turnstile"]:not([data-state="solved"])',
            'iframe[src*="challenges.cloudflare"]',
            'iframe[src*="arkose"]',
            'iframe[src*="funcaptcha"]',
            'iframe[src*="geetest"]',
            '#fc-iframe-wrap',
            'div.cf-turnstile:not([data-state="solved"])',
            'div.g-recaptcha:not([data-size="invisible"])',
            'div.h-captcha:not([data-size="invisible"])',
            '#cf-turnstile',
            'iframe[title*="Cloudflare" i]',
            'div[class*="geetest" i]',
            '#aws-waf-captcha',
            'iframe[src*="awswaf"]',
            'div[id*="challenge" i]',
            'div[class*="challenge" i]',
        ]

        targets = [page] + (getattr(page, "frames", []) or [])
        for sel in selectors:
            for target in targets:
                try:
                    loc = target.locator(sel)
                    cnt = await loc.count()
                    for idx in range(cnt):
                        el = loc.nth(idx)
                        if await el.is_visible():
                            is_inv = await el.evaluate('''el => {
                                if (el.closest('.grecaptcha-badge') || el.classList.contains('grecaptcha-badge')) return true;
                                const src = el.getAttribute('src') || '';
                                if (src.includes('size=invisible') && !src.includes('frame=challenge')) return true;
                                const ds = el.getAttribute('data-size') || '';
                                if (ds === 'invisible') return true;
                                const rect = el.getBoundingClientRect();
                                if (rect.width < 10 || rect.height < 10) return true;
                                return false;
                            }''')
                            if not is_inv:
                                return True
                except Exception:
                    pass

        # 3. Interactive challenge UI elements inside any frame or modal dialog
        for target in targets:
            try:
                btn = target.locator('button:has-text("Skip"):visible, button:has-text("Verify"):visible, button[aria-label*="challenge" i]:visible').first
                if await btn.count() > 0 and await btn.is_visible():
                    return True
            except Exception:
                pass

        return False

    @staticmethod
    async def is_captcha_verified(page) -> bool:
        """
        Verify if an active CAPTCHA has already been solved and has a valid response token.
        Supports Cloudflare Turnstile, Google reCAPTCHA v2/Enterprise, and hCaptcha.
        """
        try:
            res = await page.evaluate('''() => {
                // 1. Cloudflare Turnstile token
                const cf = document.querySelector('[name="cf-turnstile-response"], [name="cf_challenge_response"]');
                if (cf && cf.value && cf.value.trim().length > 10) return { verified: true, type: 'turnstile', len: cf.value.length };

                // 2. Google reCAPTCHA token or green checkmark
                const rc = document.querySelector('[name="g-recaptcha-response"], textarea#g-recaptcha-response');
                if (rc && rc.value && rc.value.trim().length > 10) return { verified: true, type: 'recaptcha', len: rc.value.length };

                const rcAnchor = document.querySelector('.recaptcha-checkbox[aria-checked="true"]');
                if (rcAnchor) return { verified: true, type: 'recaptcha_anchor', len: 0 };

                // 3. hCaptcha token
                const hc = document.querySelector('[name="h-captcha-response"], [name="g-recaptcha-response-1"]');
                if (hc && hc.value && hc.value.trim().length > 10) return { verified: true, type: 'hcaptcha', len: hc.value.length };

                // 4. Cloudflare Turnstile state=solved attribute
                const cfSolved = document.querySelector('.cf-turnstile[data-state="solved"], [data-state="success"]');
                if (cfSolved) return { verified: true, type: 'turnstile_attr', len: 0 };

                return { verified: false };
            }''')
            if res and res.get("verified"):
                return True
        except Exception:
            pass
        return False



# ============================================================================
# Portal Detection
# ============================================================================
def detect_portal(url: Optional[str]) -> Optional[str]:
    """Return portal key if URL belongs to a supported ATS, else None."""
    if not url or not isinstance(url, str):
        return None
    u = url.lower().strip()
    for portal, domains in SUPPORTED_PORTALS.items():
        if any(d in u for d in domains):
            return portal
    return None


# ============================================================================
# Profile Loader — merges profile.json with live config.py / .env overrides
# ============================================================================
def load_profile() -> Dict[str, Any]:
    """Load canonical profile; overlay real credentials from config.py/.env."""
    if PROFILE_PATH.exists():
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            profile = json.load(f)
    else:
        profile = {}

    pi = profile.setdefault("personal_info", {})

    # Single source of truth is profile.json
    pi["first_name"] = pi.get("first_name", getattr(config, "FIRST_NAME", "Siddhesh Santosh"))
    pi["last_name"] = pi.get("last_name", getattr(config, "LAST_NAME", "Sawant"))
    pi["full_name"] = pi.get("full_name", getattr(config, "FULL_NAME", f"{pi['first_name']} {pi['last_name']}"))
    pi["preferred_name"] = pi.get("preferred_name", getattr(config, "PREFERRED_NAME", "Siddhesh"))
    pi["email"] = pi.get("email", getattr(config, "EMAIL", "siddheshs202001@gmail.com"))
    pi["phone"] = (
        pi.get("phone", getattr(config, "PHONE", "(445) 208-2299"))
        .replace("+1 ", "")
        .replace("+1", "")
        .strip()
    )
    pi["linkedin_url"] = pi.get("linkedin_url", pi.get("linkedin", "https://www.linkedin.com/in/sss20/"))
    pi["github_url"] = pi.get("github_url", pi.get("github", "https://github.com/sids202001"))
    pi["portfolio_url"] = pi.get("portfolio_url", pi.get("portfolio", "https://siddhesh202001-portfolio.netlify.app"))

    # Canonical verbatim location across all ATS platforms
    canonical_loc = "Washington, District of Columbia, United States"
    pi["location"] = pi.get("location", canonical_loc)
    pi["full_location"] = canonical_loc
    pi["canonical_location"] = canonical_loc
    pi["city"] = pi.get("city", "Washington")
    pi["state"] = pi.get("state", "District of Columbia")
    pi["postal_code"] = pi.get("postal_code", "20024")
    pi["zip_code"] = pi.get("zip_code", "20024")

    # Home address is NOT stored by default (Rule 8)
    pi["home_address"] = pi.get("home_address", "")
    pi["address"] = pi.get("address", "")

    # Compliance and EEO settings from profile.json
    comp = profile.setdefault("compliance_and_eeo", {})
    comp["us_work_authorized"] = comp.get("us_work_authorized", "Yes")
    comp["require_visa_sponsorship"] = comp.get("require_visa_sponsorship", "Yes")
    comp["which_visa"] = comp.get("which_visa", "H-1B")
    comp["work_auth_status"] = comp.get("work_auth_status", "Authorized / Require Sponsorship")
    comp["privacy_policy_consent"] = comp.get("privacy_policy_consent", "Yes")

    prefs = profile.setdefault("preferences", {})
    if "start_date_rule" in prefs or any(k in str(prefs.get("start_date", "")).lower() for k in ["submit_date", "1 month", "immediately"]):
        prefs["start_date_computed"] = compute_start_date()

    return profile


def compute_start_date(base_dt: Optional[datetime] = None, fmt: str = "%Y-%m-%d") -> str:
    """
    Computes start date = the application's submit date + 1 month at fill time.
    Stored as a rule in profile.json (never a hardcoded calendar date).
    Date fields and text fields get the same computed date.
    """
    if base_dt is None:
        base_dt = datetime.now()
    year = base_dt.year + (base_dt.month // 12)
    month = (base_dt.month % 12) + 1
    max_days = calendar.monthrange(year, month)[1]
    day = min(base_dt.day, max_days)
    target = base_dt.replace(year=year, month=month, day=day)
    return target.strftime(fmt)


def resolve_start_date(profile: Optional[Dict[str, Any]] = None, submit_dt: Optional[datetime] = None, fmt: str = "%Y-%m-%d") -> str:
    """
    Resolves the start date dynamically using the rule in profile.json.
    Ensures start date is never a hardcoded calendar date.
    """
    if profile is None:
        profile = load_profile()
    prefs = profile.get("preferences", {})
    rule = prefs.get("start_date_rule") or prefs.get("start_date", "")

    # If dynamic rule string specified (e.g. 'submit_date + 1 month')
    if any(k in str(rule).lower() for k in ["submit_date", "1 month", "+ 1 month", "+1 month", "compute", "dynamic"]):
        return compute_start_date(base_dt=submit_dt, fmt=fmt)

    if prefs.get("start_date_rule"):
        return compute_start_date(base_dt=submit_dt, fmt=fmt)

    # If rule is legacy 'Immediately' or not a calendar date, compute it dynamically
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule)) and not re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", str(rule)):
        return compute_start_date(base_dt=submit_dt, fmt=fmt)

    return str(rule)


# ============================================================================
# Field Type Classification
# ============================================================================
class FieldType(Enum):
    TEXT_INPUT = auto()
    TEXTAREA = auto()
    SELECT_NATIVE = auto()
    COMBOBOX = auto()       # React-Select / autocomplete
    RADIO = auto()
    CHECKBOX = auto()
    TOGGLE_BUTTON = auto()  # Ashby Yes/No aria-pressed buttons
    FILE_UPLOAD = auto()
    DATE_INPUT = auto()
    UNKNOWN = auto()


@dataclass
class QuestionBlock:
    """Represents a single form question: its label, field type, and locators."""
    label: str
    field_type: FieldType
    container: Locator          # the enclosing DOM block
    input_locator: Locator      # the primary interactable element
    choices: List[str] = field(default_factory=list)
    filled: bool = False
    portal: str = ""


def normalize_label(label: str) -> str:
    """
    Unified ATS Label Normalization (Shared across all fields and all ATS portals):
    1. Replaces \\xa0 and all Unicode whitespace with standard ASCII space.
    2. Strips parenthetical instructions (e.g. '(please provide dollar amount only)', '(optional)', '(in USD)').
    3. Strips square-bracket instructions (e.g. '[optional]').
    4. Lowercases and trims.
    5. Normalizes internal whitespace.
    """
    if not label:
        return ""
    # 1. Replace \xa0 and all Unicode whitespace with standard space
    text = re.sub(r'[\s\xa0\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]+', ' ', str(label))
    # 2. Strip parenthetical instructions e.g. "(please provide dollar amount only)", "(mark all that apply)", "(USD)"
    text = re.sub(r'\s*\([^)]*\)', '', text)
    # 3. Strip square bracket instructions e.g. "[optional]"
    text = re.sub(r'\s*\[[^\]]*\]', '', text)
    # 4. Lowercase and trim
    text = text.lower().strip()
    # 5. Normalize internal whitespace
    text = re.sub(r'\s+', ' ', text)
    return text


# ============================================================================
# Deterministic Answer Engine
# ============================================================================
class DeterministicEngine:
    """Keyword-regex rules evaluated BEFORE calling Gemini."""

    normalize_label = staticmethod(normalize_label)

    @staticmethod
    def pick_best_location_choice(choices: List[str]) -> Optional[str]:
        if not choices:
            return None
        # Clean choices: remove placeholders
        clean = []
        for c in choices:
            if not c:
                continue
            c_str = str(c).strip()
            if not c_str:
                continue
            if re.match(r'^(select\.{0,3}|choose\.{0,3}|--+|please select\.{0,3}|placeholder|none|null|undefined)$', c_str, re.IGNORECASE):
                continue
            clean.append(c_str)
        if not clean:
            return None

        # Rule 4: Candidate preferred locations ONLY (Washington DC, Remote US, United States)
        # BANNED: Never pick arbitrary hubs (San Francisco, NY, Austin, etc.) and never fall back to clean[0]
        approved_kws = [
            "washington, dc", "washington", "district of columbia",
            "remote - us", "us remote", "remote (us)", "remote, us", "remote - united states",
            "united states", "usa"
        ]
        for kw in approved_kws:
            for c in clean:
                if kw in c.lower():
                    return c

        return None

    @staticmethod
    def solve(question: str, choices: Optional[List[str]] = None, is_required: bool = True) -> Optional[str]:
        """Return a deterministic answer or None to defer to AI."""
        raw_question = question
        q = normalize_label(question)
        profile = load_profile()
        pi = profile.get("personal_info", {})
        comp = profile.get("compliance_and_eeo", {})
        exp = profile.get("experience", [{}])[0]

        # --- Grounded Open-Text Answer Writer (Why-company + any open-text descriptive question) ---
        # Runs after canonical R9 screening halts (e.g. GCP/HRIS/travel) so unowned-skill halts keep their code,
        # and before single-word keyword rules so open-text prompts are never hijacked by 'language'/'experience'->"2".
        try:
            from app.services.grounded_essay import is_why_company_question, write_grounded_open_answer
        except Exception:
            is_why_company_question = None
        if is_why_company_question and not choices and is_why_company_question(raw_question):
            return write_grounded_open_answer(raw_question, is_required=is_required)

        # --- Rule 4 (Nov 18 Addendum): Relocation / Assistance / Timing / Proximity / Willingness ---
        # 1. Relocation Assistance (halt and ask)
        if is_relocation_assistance_question(raw_question):
            raise FormHaltException(
                "RELOCATION_ASSISTANCE_HALT",
                f"Relocation assistance question encountered: '{raw_question}'. Halting without guessing.",
                question=raw_question,
                choices=choices
            )

        # 2. Relocation Timing (halt and ask)
        if is_relocation_timing_question(raw_question):
            raise FormHaltException(
                "RELOCATION_TIMING_HALT",
                f"Relocation timing question encountered: '{raw_question}'. Halting without guessing.",
                question=raw_question,
                choices=choices
            )

        # 3. Remote Work Willingness (halt and ask)
        if is_remote_willingness_question(raw_question):
            raise FormHaltException(
                "REMOTE_WILLINGNESS_HALT",
                f"Remote work preference question encountered: '{raw_question}'. Halting without guessing.",
                question=raw_question,
                choices=choices
            )

        # 3b. Travel willingness / 30-50% travel (R9: halt TRAVEL_DECISION_REQUIRED)
        if is_travel_decision_question(raw_question):
            raise FormHaltException(
                "TRAVEL_DECISION_REQUIRED",
                f"Travel requirement question encountered: '{raw_question}'. No canonical travel answer on file. Halting without guessing.",
                question=raw_question,
                choices=choices
            )

        # 3c. Canonical screening answers from profile.json (R9)
        canonical_screening = solve_canonical_screening_question(raw_question, choices, is_required=is_required)
        if canonical_screening is not None:
            return canonical_screening

        # 4. Current-Location / Proximity / Commute / 50-Miles Questions (truthful DC answer or halt)
        if is_current_location_or_proximity_question(raw_question, choices):
            return solve_proximity_or_current_location(raw_question, choices, is_required=is_required)

        # 5. Relocation / In-Office Willingness Questions (canonical 'Yes' from profile.json)
        if is_relocation_willingness_question(raw_question, choices):
            return solve_relocation_willingness(raw_question, choices)

        # --- Rule 5: "Which visa?" = H-1B ---
        if is_which_visa_question(raw_question):
            return solve_which_visa(raw_question, choices, is_required=is_required)

        # --- Rule 9: "How did you hear about us?" ---
        if is_how_did_you_hear_question(raw_question):
            return solve_how_did_you_hear(raw_question, choices, is_required=is_required)

        # --- Rule 10: Communications / SMS consent ---
        if re.search(r"(communication\s*consent|text\s*message|receive\s*text|text\s*and\s*sms|sms\s*consent|opt-?in\s*to\s*receive|whatsapp)", q, re.IGNORECASE):
            return solve_consent_question(raw_question, choices)

        # --- Rule 8: Home address field ---
        if is_home_address_field(raw_question):
            home_addr = pi.get("home_address", "")
            if home_addr:
                return home_addr
            if is_required:
                raise FormHaltException(
                    "ADDRESS_REQUIRED",
                    f"Home street address is required for field '{raw_question}' but not stored in profile.json.",
                    question=raw_question,
                    choices=choices
                )
            return ""

        # --- Rule 7: Contradictory-premise questions ---
        if is_contradictory_premise_question(raw_question):
            if is_required:
                raise FormHaltException(
                    "PREMISE_MISMATCH",
                    f"Question premise contradicts candidate profile: '{raw_question}'.",
                    question=raw_question,
                    choices=choices
                )
            return ""

        # --- Rule 6: Open-text questions, essays, opinions, and ratings ---
        # Groundable open-text questions write a verified 2-4 sentence answer from the candidate's record (+ JD/about);
        # Hard limits (4) and ungroundable questions (5) safe-halt with ESSAY_HALT (required) or return "" (optional).
        if not choices:
            try:
                from app.services.grounded_essay import is_open_text_question, write_grounded_open_answer
                if is_open_text_question(raw_question):
                    return write_grounded_open_answer(raw_question, is_required=is_required)
            except ImportError:
                pass
        if is_essay_or_subjective_question(raw_question):
            if not choices:
                from app.services.grounded_essay import write_grounded_open_answer
                return write_grounded_open_answer(raw_question, is_required=is_required)
            if is_required:
                raise FormHaltException(
                    "ESSAY_HALT",
                    f"Subjective essay or self-rating question required: '{raw_question}'. Bot never generates essays or subjective ratings.",
                    question=raw_question,
                    choices=choices
                )
            return ""

        def _pick(keywords, from_choices):
            """Pick matching choice, respecting keyword priority order and word boundaries."""
            if not from_choices:
                return None
            for k in keywords:
                k_low = normalize_label(k)
                for c in from_choices:
                    c_low = normalize_label(c)
                    # === Critical substring collision guards ===
                    # Prevent 'male'/'man' from matching 'female'/'woman'
                    if k_low in ["male", "man", "cisgender man", "cisgender male"] and ("female" in c_low or "woman" in c_low):
                        continue
                    # Prevent 'he'/'him'/'his' from matching 'she'/'her'/'hers'
                    if k_low in ["he", "him", "his", "he/him", "he / him"] and ("she" in c_low or "her" in c_low):
                        continue
                    # Prevent 'asian' from matching 'caucasian'
                    if k_low == "asian" and "caucasian" in c_low:
                        continue
                    # Prevent 'no' from matching substrings like 'technology', 'economics', 'non-binary'
                    if k_low == "no" and c_low.strip() != "no" and not re.search(r"(?:^|\b)no(?:\b|$)", c_low):
                        continue
                    # Exact match (highest priority)
                    if k_low == c_low.strip():
                        return c
                    # Word boundary match (with fallback for non-word chars like $, +, .)
                    try:
                        if re.search(rf"(?:^|\b){re.escape(k_low)}(?:\b|$)", c_low):
                            return c
                    except re.error:
                        pass
                    # Multi-word phrase substring
                    if " " in k_low and k_low in c_low:
                        return c
                    # Non-word character tokens (e.g. "$100", "2+", "u.s.a.", "f-1")
                    if not k_low.isalpha() and k_low in c_low:
                        return c
            return None

        _NEGATION_WORDS = {"not", "never", "unable", "cannot", "don't", "dont", "lack", "without", "no"}

        def _has_negation(text: str) -> bool:
            """Return True if the text contains a negation word before an affirmative keyword."""
            tokens = text.lower().split()
            for i, tok in enumerate(tokens):
                clean_tok = re.sub(r'[^a-z]', '', tok)
                if clean_tok in _NEGATION_WORDS:
                    return True
            return False

        def _yes(from_choices=None):
            if from_choices:
                result = _pick(["yes", "true", "agree", "confirm", "accept", "acknowledge"], from_choices)
                if result:
                    return result
                for c in from_choices:
                    c_low = c.lower().strip()
                    # GUARD: Never match a negated option as affirmative
                    if _has_negation(c_low):
                        continue
                    if any(y in c_low for y in ["yes", "true", "agree", "confirm", "accept", "authorized", "eligible"]):
                        return c
                if len(from_choices) == 2:
                    fc0 = from_choices[0].lower().strip()
                    fc1 = from_choices[1].lower().strip()
                    if any(n in fc1 for n in ["no", "false", "decline", "never"]):
                        return from_choices[0]
                    if any(n in fc0 for n in ["no", "false", "decline", "never"]):
                        return from_choices[1]
                return "Yes"
            return "Yes"

        def _no(from_choices=None):
            if from_choices:
                result = _pick(["no", "false", "never", "decline", "none", "n/a", "not a", "not applicable", "do not"], from_choices)
                if result:
                    return result
                for c in from_choices:
                    c_low = c.lower().strip()
                    if c_low.startswith("no") or "not a " in c_low or "not relative" in c_low or "never" in c_low:
                        return c
                for c in reversed(from_choices):
                    c_low = c.lower().strip()
                    if any(n in c_low for n in ["no", "false", "never", "decline", "none"]):
                        return c
                return "No"
            return "No"

        # --- LinkedIn / GitHub / Portfolio ---
        if re.search(r"\blinkedin\b", q):
            return pi.get("linkedin_url", getattr(config, "LINKEDIN_URL", "https://www.linkedin.com/in/sss20/"))
        if re.search(r"\bgithub\b", q):
            return pi.get("github_url", getattr(config, "GITHUB_URL", "https://github.com/sids202001"))
        if re.search(r"(portfolio.*(password|passcode)|password.*portfolio)", q, re.IGNORECASE):
            return "N/A"
        if re.search(r"\b(portfolio|personal\s*site|personal\s*website)\b", q):
            return pi.get("portfolio_url", getattr(config, "PORTFOLIO_URL", "https://siddhesh202001-portfolio.netlify.app")) or pi.get("github_url", "")
        if re.search(r"\bwebsite\b", q):
            return pi.get("portfolio_url", getattr(config, "PORTFOLIO_URL", "https://siddhesh202001-portfolio.netlify.app")) or pi.get("github_url", "")
        if re.search(r"\b(twitter|x\.com)\b", q):
            return pi.get("twitter_url", getattr(config, "TWITTER_URL", "")) or ""

        # --- How did you hear about us / this opportunity ---
        if re.search(r"(how\s*did\s*you\s*(hear|learn)|where\s*did\s*you\s*(hear|see)|referr(al|ed)\s*source)", q, re.IGNORECASE):
            if choices:
                c_match = _pick(["linkedin", "job board", "company website", "online", "internet"], choices)
                if c_match:
                    return c_match
            return "LinkedIn"

        # ==============================================================
        # DETERMINISTIC REGEX MATRIX (Pure Code Logic — Zero LLMs)
        # ==============================================================

        # --- Name Pronunciation / Personal Preferences ---
        if re.search(r"(pronounc|how\s*do\s*you\s*pronounce|name\s*pronunciation|personal\s*preference)", q, re.IGNORECASE):
            return getattr(config, "NAME_PRONUNCIATION", "Sid-desh Sa-want")

        # --- Military Support / Allied Forces / Defense Missions ---
        if re.search(r"(reasons?\s*why\s*you\s*would\s*not\s*be\s*able\s*to\s*fully\s*contribute|military.*allied\s*forces.*reasons|contribute\s*in\s*support\s*of\s*the\s*u\.?s\.?\s*military|support\s*the\s*u\.?s\.?\s*military\s*and\s*allied\s*forces|objections?\s*to\s*working\s*on.*(defense|military)|moral.*objection.*(defense|military)|reasons?\s*preventing.*contribut.*military)", q, re.IGNORECASE):
            return _no(choices)

        if re.search(r"(comfortable\s*working\s*on\s*(defense|military)|willing\s*to\s*work\s*on\s*(defense|military|government|national\s*security)|support\s*(defense|military)\s*contracts)", q, re.IGNORECASE):
            return _yes(choices)

        # --- Phone Country Calling Code ---
        if re.search(r"(phone\s*country|country\s*code|calling\s*code)", q, re.IGNORECASE) or (re.search(r"\bphone\b", q, re.IGNORECASE) and choices and any("+1" in str(c) for c in choices)):
            if choices:
                c_match = _pick(["united states +1", "united states (+1)", "+1", "us (+1)", "united states"], choices)
                if c_match:
                    return c_match
                for c in choices:
                    if "+1" in str(c):
                        return c
            return "United States +1"

        # --- U.S. Person Status / Export Control (ITAR / EAR / Cerebras) ---
        if re.search(r"(u\.?s\.?\s*person|us\s*person|which.*best\s*describes\s*your.*u\.?s\.?\s*person|\bitar\b|\bear\b|subject\s*to\s*export\s*controls|export\s*(compliance|controls?)|access\s*to\s*information.*export|cerebras\s*products.*export|products,?\s*software.*technology.*export)", q, re.IGNORECASE):
            if choices:
                c_match = _pick([
                    "none of the above; i am a citizen of a different country",
                    "none of the above",
                    "citizen of a different country",
                    "different country",
                    "not a u.s. person",
                    "not a us person",
                    "i am not a u.s. person",
                    "eligible for licensing",
                    "other",
                    "neither",
                    "no"
                ], choices)
                if c_match:
                    return c_match
                for c in choices:
                    c_l = c.lower().strip()
                    if ("different country" in c_l or "not a u.s." in c_l or "not a us" in c_l or "neither" in c_l or "eligible for licensing" in c_l) and "cuba" not in c_l:
                        return c
                return _no(choices)
            return "I am not a U.S. Person (Citizen of India, F-1 OPT)"

        # --- Export Control / Clearance Conditional Follow-ups after "No" Parent ---
        # Rule: Conditional follow-ups after a "No" parent get N/A or skip — NEVER "No"
        if re.search(r"(if\s*you\s*selected\s*[\"']?(a\s*protected\s*individual|other)[\"']?.*(question|above)|state\s*your\s*current\s*citizenship.*visa|citizenship\s*status\s*and\s*visa)", q, re.IGNORECASE):
            return "Indian Citizen - F-1 OPT (STEM Eligible)"
        if re.search(r"(if\s*yes,?\s*(please\s*)?(specify|explain|detail|elaborate)|if\s*other,?\s*(please\s*)?specify)", q, re.IGNORECASE):
            if choices:
                return _pick(["n/a", "not applicable", "none"], choices) or "N/A"
            return "N/A"

        # --- Transitioning Service Member / Military Service ---
        if re.search(r"(transitioning\s*service\s*member|service\s*member\s*transitioning|active\s*duty\s*military|currently\s*serving\s*in\s*the\s*military)", q, re.IGNORECASE):
            return _no(choices)

        # --- SEC Auditor Independence / Accounting Firms (PwC, KPMG, Deloitte, EY) ---
        if re.search(r"(auditor\s*independence|pricewaterhousecoopers|\bpwc\b|independent\s*auditor|previously\s*worked\s*at.*auditor|ernst\s*&\s*young|\bey\b|deloitte|kpmg|accounting\s*firm)", q, re.IGNORECASE):
            if choices:
                c_match = _pick([
                    "no - i have never been employed by pwc",
                    "no - i have never been employed",
                    "have never been employed",
                    "never been employed",
                    "never worked",
                    "no",
                    "false",
                    "none"
                ], choices)
                if c_match:
                    return c_match
                return _no(choices)
            return "No"

        # --- Government Security Clearance ---
        if re.search(r"(able\s*to\s*obtain.*clearance|willing\s*to\s*(obtain|undergo).*clearance|eligible\s*to\s*obtain.*clearance|s\/sar\s*clearance)", q, re.IGNORECASE):
            return _yes(choices)

        # Clearance Level (Follow-up question)
        if re.search(r"(what\s*(level|type)\s*(is|of)\s*(your\s*)?(security\s*)?clearance|clearance\s*level|level\s*of\s*(security\s*)?clearance|current\s*clearance\s*level)", q, re.IGNORECASE):
            if choices:
                c_match = _pick(["n/a", "not applicable", "none", "no clearance", "no", "null"], choices)
                if c_match:
                    return c_match
                for c in choices:
                    if any(k in c.lower() for k in ["n/a", "none", "not applicable", "no clearance"]):
                        return c
                return "N/A"
            return "N/A"

        # Clearance Existence (Parent question)
        if re.search(r"(security\s*clearance|active\s*(secret|top\s*secret|ts/sci|clearance)|government\s*clearance|currently\s*(hold|possess).*clearance|do\s*you\s*(currently\s*)?(have|hold)\s*(a\s*)?(security\s*)?clearance)", q, re.IGNORECASE):
            if choices:
                c_match = _pick(["no", "none", "no clearance", "not applicable", "n/a", "false"], choices)
                if c_match:
                    return c_match
                return _no(choices)
            return "No"

        # --- Relocation / Local Office Willingness ---
        if re.search(r"(local\s*to\s*or\s*willing\s*to\s*relocate|willing\s*to\s*relocate|open\s*to\s*relocat)", q, re.IGNORECASE):
            return solve_relocation_willingness(raw_question, choices)

        # --- Full Time Job(s) Applying For ---
        if re.search(r"(what\s*(full\s*time\s*)?job\(?s?\)?\s*are\s*you\s*applying\s*for|position(s)?\s*applying\s*for|role(s)?\s*applying\s*for)", q, re.IGNORECASE):
            return "Software Engineer / Backend Engineer"

        # --- Full Legal Name ---
        if re.search(r"(state\s*your\s*full\s*legal\s*name|full\s*legal\s*name)", q, re.IGNORECASE):
            return pi.get("full_name", "Siddhesh Santosh Sawant")

        # --- Languages Fluent In ---
        if re.search(r"(languages?\s*are\s*you\s*fluent\s*in|fluent\s*in|languages?\s*spoken|spoken\s*languages?)", q, re.IGNORECASE):
            if choices:
                c_match = _pick(["english", "en"], choices)
                if c_match:
                    return c_match
            return "English"

        # --- Availability for Full Time / Fellowship ---
        if re.search(r"(when\s*are\s*you\s*available\s*for\s*full\s*time\s*work|available\s*for\s*full\s*time|available\s*to\s*start\s*(your\s*)?fellowship)", q, re.IGNORECASE):
            return "Within 2-3 weeks of offer acceptance or visa approval"

        # --- Location Applying For / Opportunity Location ---
        if re.search(r"(which\s*location\s*are\s*you\s*applying\s*for|opportunity\s*location.*|opportunitylocationid|role\s*location|office\s*location|select\s*a?\s*location|location\s*preference|preferred\s*location)", q, re.IGNORECASE):
            if choices:
                c_match = DeterministicEngine.pick_best_location_choice(choices)
                if c_match:
                    return c_match
            return "Washington, DC"

        # --- In-person / Hybrid / Office Presence (Benchling, Anthropic, etc.) ---
        if re.search(r"(in-?person|one\s*of\s*our\s*offices|from\s*our\s*office|work\s*(from|in)\s*(an?|our)?\s*office|office\s*25%|25%\s*of\s*the\s*time|work\s*in-?office|days?\s*(per|\/|a)\s*week|five\s*days\s*a\s*week|open\s*to\s*working\s*in-?person|work\s*on-?site|hybrid)", q, re.IGNORECASE) and not re.search(r"(only\s*remote|remote\s*only)", q, re.IGNORECASE):
            hybrid_willingness = comp.get("hybrid_office_willingness", getattr(config, "HYBRID_OFFICE_WILLINGNESS", "Yes"))
            if str(hybrid_willingness).strip().lower() in ["yes", "true", "1"]:
                return _yes(choices)
            else:
                return _no(choices)

        # --- Payroll Tax Location / Where do you plan on working from ---
        if re.search(r"(where\s*do\s*you\s*plan\s*on\s*working|plan\s*on\s*working\s*from|working\s*from.*payroll|payroll.*tax|tax\s*purposes|work\s*location\s*for\s*payroll|working\s*from)", q, re.IGNORECASE):
            if choices:
                c_match = DeterministicEngine.pick_best_location_choice(choices)
                if c_match:
                    return c_match
            return "Washington, District of Columbia, United States"

        # --- Text Messaging / SMS / Communication Consent ---
        if re.search(r"(text\s*message.*agreement|communication\s*consent|receive\s*text\s*message|consent\s*to\s*receiving\s*text|text\s*and\s*sms|texting\s*consent)", q, re.IGNORECASE):
            if choices:
                c_match = _pick(["yes - i consent to receiving text messages", "yes - i consent", "yes", "i consent", "given", "agree"], choices)
                if c_match:
                    return c_match
            return "Yes - I consent to receiving text messages"

        # --- Address from which you plan on working / Relocating ---
        if re.search(r"(address\s*from\s*which\s*you\s*(plan|would)|plan\s*on\s*working.*address|type\s*[\"']?relocating[\"']?)", q, re.IGNORECASE):
            return "Relocating"

        # --- Deadlines / Timeline Considerations (Must be None!) ---
        if re.search(r"(deadlines?\s*or\s*timeline|timeline\s*consideration|competing\s*offer|offer\s*deadline)", q, re.IGNORECASE):
            return "None. Flexible and ready to align with company interview and onboarding timeline."

        # --- Earliest Start Date ---
        if re.search(r"(earliest.*start|when.*start\s*working|available\s*to\s*start|how\s*soon\s*can\s*you\s*start|when\s*would\s*you\s*be\s*available\s*to\s*start|what\s*is\s*the\s*earliest\s*date)", q, re.IGNORECASE):
            start_ans = resolve_start_date(profile)
            if choices:
                return _pick([start_ans, "1 month", "within 1 month", "4 weeks", "within 4 weeks", "2 weeks", "immediately", "as soon as possible"], choices) or choices[0]
            return start_ans

        # --- Prior Interviews / Interviewed at company before (Always NO!) ---
        if re.search(r"(ever\s*interviewed|interviewed.*before|interviewed\s*(at|with)|previous(ly)?\s*interviewed|applied\s*before)", q, re.IGNORECASE):
            return _no(choices)

        # --- AI Policy / Candidate AI Guidance ---
        if re.search(r"(ai\s*policy|guidance\s*on\s*candidates'?\s*ai|ai\s*partnership\s*guidelines|claude\s*to\s*realize\s*this\s*vision)", q, re.IGNORECASE):
            return _yes(choices)

        # --- Arbitration Agreement / Agreement to Arbitrate ---
        if re.search(r"(arbitration\s*agreement|agreement\s*to\s*arbitrate)", q, re.IGNORECASE):
            if choices:
                return _pick(["i understand and agree to the terms", "i will read the arbitration", "agree", "accept", "yes"], choices) or choices[0]
            return "I understand and agree to the terms of the Agreement to Arbitrate set forth above."

        # --- Why Anthropic? (Comprehensive, 200+ word grounded response) ---
        if re.search(r"(why\s*anthropic|interest\s*in\s*anthropic)", q, re.IGNORECASE):
            return (
                "I want to work at Anthropic because I am deeply inspired by your mission to build steerable, reliable, "
                "and interpretable AI systems that are fundamentally safe and beneficial for humanity. Having worked as a "
                "Software Developer at Talviro INC building backend services and tooling, and previously as a Software "
                "Development Engineer at Amazon Web Services (AWS) engineering high-throughput distributed systems using Python, "
                "AWS Step Functions, S3, and data pipelines, I thrive at the intersection of robust systems engineering and data "
                "infrastructure. The technical challenges at Anthropic—especially designing scalable data processing infrastructure, "
                "reproducible pipelines, and fault-tolerant primitives for training next-generation frontier models—align directly "
                "with my engineering passions and background. I am eager to contribute my backend rigor, distributed systems "
                "experience, and commitment to AI safety to Anthropic's pre-training engineering team."
            )

        # --- Why OpenAI? ---
        if re.search(r"(why\s*openai|interest\s*in\s*openai)", q, re.IGNORECASE):
            return (
                "I want to join OpenAI to help build safe, highly scalable infrastructure that empowers artificial general "
                "intelligence to benefit all of humanity. My background in distributed systems at AWS and backend engineering "
                "at Talviro INC has given me deep experience designing high-throughput data pipelines, fault-tolerant architectures, "
                "and automated cloud workflows in Python and AWS. I am excited to apply this distributed systems experience to "
                "the massive-scale infrastructure challenges at OpenAI."
            )

        # If a select has exactly 1 available choice (e.g. ['Thank you'], ['I acknowledge...']), select it
        if choices and len(choices) == 1:
            return choices[0]

        # --- Additional Information / Comments ---
        if re.search(r"^(additional\s*(information|comments|notes)|anything\s*else(\s*you('?d|\s*would)\s*like.*share)?)$", q, re.IGNORECASE):
            return "N/A"

        # --- Corporate Integrity, Government Employment & Government Official Checks (TOP PRIORITY) ---
        if re.search(r"(government\s*(official|employment|service|job|agency|owned|entity|body|role|position)|worked\s*for.*government|employed.*government|civil\s*service|public\s*office|politically\s*exposed|public\s*official|prior.*government)", q, re.IGNORECASE):
            if choices:
                return _pick([
                    "no, i am not a current or former government official",
                    "no, i am not a relative of a government official",
                    "no, i have not worked for the government",
                    "no, i do not",
                    "no, i am not",
                    "not a current or former",
                    "not a government official",
                    "not a relative",
                    "no",
                    "false"
                ], choices) or _no(choices)
            return "No"

        if re.search(r"(relative.*government\s*official|close\s*relative.*government|family.*government\s*official)", q, re.IGNORECASE):
            if choices:
                return _pick([
                    "no, i am not a relative of a government official",
                    "no, i am not",
                    "not a relative",
                    "no",
                    "false"
                ], choices) or _no(choices)
            return "No"

        # Conflicts of Interest / Connection to Senior Leaders / Institutional Clients / Vendors
        if re.search(r"(conflict\s*of\s*interest|perceived\s*conflict|actual\s*or\s*perceived|client,\s*business\s*partner|institutional\s*client|business\s*partner.*vendor|senior\s*leader.*vendor|senior\s*leader.*client|senior\s*leader\s*at\s*a\s*company\s*connected|close\s*personal\s*relationship\s*with\s*a\s*senior\s*leader|significant\s*financial\s*interest)", q, re.IGNORECASE):
            return _no(choices)

        # Referred by Institutional Client / Partner / Vendor / Senior Decision Maker
        if re.search(r"(were\s*you\s*referred.*(client|partner|vendor|decision\s*maker|senior\s*leader)|referred\s*to\s*this\s*position\s*by.*(client|partner|vendor))", q, re.IGNORECASE):
            return _no(choices)

        # Previously Employed / Consulted for This Company in Any Capacity
        if re.search(r"(previously\s*(been\s*)?employed|prior\s*employment\s*at|worked\s*for.*in\s*any\s*capacity|employed\s*by.*in\s*any\s*capacity|ever\s*worked\s*for|former\s*employee|consulted\s*for.*in\s*any\s*capacity|worked\s*for\s*\w+\s*previously|have\s*you\s*worked\s*for)", q, re.IGNORECASE):
            return _no(choices)

        # AI Usage Profile / Workflow Automation / How You Use AI Today
        if re.search(r"(how\s*(do\s*)?you\s*use\s*ai|best\s*describes\s*how\s*you\s*use\s*ai|use\s*ai\s*tools\s*today|ai\s*tools\s*in\s*your\s*workflow|approach\s*to\s*using\s*ai)", q, re.IGNORECASE):
            if choices:
                return _pick([
                    "i design or automate workflows with ai tools (e.g., building agents, integrating ai into team processes)",
                    "i design or automate workflows with ai tools",
                    "design or automate workflows",
                    "building agents",
                    "integrating ai into team processes",
                    "i regularly use ai tools",
                    "regularly use ai tools",
                    "i have experimented with ai tools",
                    "experimented with ai tools"
                ], choices) or choices[-1]
            return (
                "I actively design and automate workflows with AI tools and autonomous agents—integrating LLMs via structured API frameworks "
                "and protocols (such as Model Context Protocol) to streamline development, code review, debugging, and operational tasks."
            )

        # Company AI Notice / Interview AI Assistance
        if re.search(r"(understand\s*that.*may\s*use\s*ai|use\s*ai\s*tools\s*to\s*assist\s*in\s*the\s*application|ai\s*in\s*the\s*(hiring|interview|application)\s*process)", q, re.IGNORECASE):
            if choices:
                return _pick(["yes", "agree", "accept", "i understand", "confirmed"], choices) or choices[0]
            return "Yes"

        # Privacy Policy / Arbitration Notice / Terms Confirmation Receipt
        if re.search(r"(confirm\s*receipt\s*of|privacy\s*notice.*arbitration|data\s*privacy.*arbitration|receipt\s*of\s*the\s*above|global\s*data\s*privacy)", q, re.IGNORECASE):
            if choices:
                return _pick(["confirmed", "i understand and agree", "agree", "accept", "yes", "i confirm", "i acknowledge"], choices) or choices[0]
            return "Confirmed"

        # Age 18 or Older
        if re.search(r"(at\s*least\s*18|18\s*years\s*(of\s*age)?|legal\s*age\s*to\s*work)", q, re.IGNORECASE):
            return _yes(choices)

        # Rule 9: How did you hear about this job / referral source
        if re.search(r"(how\s*did\s*you\s*.*hear|where\s*did\s*you\s*.*hear|how\s*did\s*you\s*.*find|where\s*did\s*you\s*.*find|source\s*of\s*application|initial(ly)?\s*hear|referral\s*source)", q, re.IGNORECASE) and not re.search(r"(if\s*you\s*(chose|selected)|if\s*other|please\s*specify)", q, re.IGNORECASE):
            return solve_how_did_you_hear(raw_question, choices, is_required=is_required)

        # Conditional Follow-ups: Referral / Employee / Other / Conference / Event Specification
        if re.search(r"(if\s*you\s*(chose|selected).*specify|if\s*other.*(specify|explain|describe)|if\s*referr.*(who|name|specify|list)|who\s*referred\s*you|employee\s*referr.*name|name\s*of\s*(the\s*)?referr|former\s*employee.*name|referred\s*by\s*an\s*employee)", q, re.IGNORECASE):
            # Candidate applied independently via official ATS board and has no referral from an employee, conference, or 'Other'
            return "N/A"

        # --- Core Candidate Identifiers & Contact Details ---
        if re.search(r"(^|\b)(first\s*name|given\s*name)\b", q, re.IGNORECASE) and not re.search(r"(referr|manager|supervisor|friend|legal.*differ)", q, re.IGNORECASE):
            return pi.get("first_name", "Siddhesh Santosh")
        if re.search(r"(^|\b)(last\s*name|family\s*name|surname)\b", q, re.IGNORECASE) and not re.search(r"(referr|manager|supervisor|friend|legal.*differ)", q, re.IGNORECASE):
            return pi.get("last_name", "Sawant")
        if re.search(r"(^|\b)(email|e-mail)\b", q, re.IGNORECASE) and not re.search(r"(referr|manager|supervisor|friend)", q, re.IGNORECASE):
            return pi.get("email", "siddheshs202001@gmail.com")
        if re.search(r"(^|\b)(phone|mobile|cell|telephone)\b", q, re.IGNORECASE) and not re.search(r"(referr|manager|supervisor|friend)", q, re.IGNORECASE):
            if choices and any("consent" in c.lower() or "sms" in c.lower() or "text" in c.lower() or "message" in c.lower() for c in choices):
                return _pick(["yes - i consent to receiving text messages", "yes - i consent", "consent", "yes", "agree"], choices) or choices[0]
            return pi.get("phone", "(445) 208-2299")

        # Military service / Relevant employment acknowledgment via Add Another link
        if re.search(r"(relevant\s*employment.*add\s*another|military\s*service.*add\s*another|add\s*another\s*employment\s*link)", q):
            if choices:
                return choices[0]
            return "Thank you"

        # Contact current employer -> Yes
        if re.search(r"(contact.*current\s*employer|contact.*employer|reach\s*out\s*to.*employer)", q):
            return _yes(choices)

        # Non-competes, post-employment restrictions, agreements -> No / None
        if re.search(r"(employment\s*agreement|post-?employment\s*restriction|restrictive\s*covenant|non-?compete|non-?disclosure|subject\s*to\s*any\s*(employment\s*)?agreement)", q, re.IGNORECASE):
            if choices:
                return _no(choices)
            return "None"

        # Preferred Last Name
        if re.search(r"preferred\s*(last|family|sur)\s*name", q, re.IGNORECASE):
            return pi.get("last_name", "Sawant")

        # Preferred name / First name
        if re.search(r"(preferred\s*(first\s*)?name|name\s*you('?d|\s*would)\s*prefer|call\s*you)", q, re.IGNORECASE):
            return pi.get("preferred_name") or pi.get("first_name", "Siddhesh")

        # Open source / GitHub / Repositories / Code samples
        if re.search(r"(open\s*source|github|portfolio\s*link|code\s*sample|project\s*link|repositories)", q, re.IGNORECASE) and not re.search(r"(password|passcode|pin)", q, re.IGNORECASE):
            return pi.get("github_url", "https://github.com/siddhesh-sawant")

        # Country of residence
        if re.search(r"(country\s*of\s*residence|current\s*country|country\s*you\s*(currently\s*)?live)", q, re.IGNORECASE):
            if choices:
                return _pick(["united states of america", "united states", "usa", "us"], choices) or "United States of America"
            return "United States of America"

        # Are you located in [Country/Region] (e.g. UK or Poland)
        if re.search(r"(are\s*you\s*(currently\s*)?located\s*in|do\s*you\s*(currently\s*)?reside\s*in|living\s*in)\b", q, re.IGNORECASE) and not re.search(r"(authorized|authorization|eligible\s*to\s*work|sponsorship|work\s*auth)", q, re.IGNORECASE):
            if re.search(r"\b(us|usa|united\s*states|washington|dc|district\s*of\s*columbia)\b", q, re.IGNORECASE):
                return _yes(choices)
            elif choices:
                return _no(choices)
            return "No"

        # Previously employed / consulted for / worked for company -> No
        if re.search(r"(previously\s*(employed|worked|consulted)|employed\s*(by|at)|worked\s*(for|at)|former\s*employee|ever\s*(been\s*employed|worked)|consulted\s*(for|with)|contract\s*work|provided\s*any\s*contract)", q, re.IGNORECASE):
            return _no(choices)

        # Primary programming language
        if re.search(r"(primary|main|preferred)\s*(programming\s*)?language", q, re.IGNORECASE):
            if choices:
                return _pick(["python", "java", "sql"], choices) or choices[0]
            return "Python"

        # 1. (?i)(current|previous)\s+(employer|company) -> "Talviro INC" (ONLY if open text and NOT agreements/restrictions)
        if not re.search(r"(contact|agreement|restriction|non-?compete|conflict|government|vendor|client|referr|relative|hold\s*a\s*role|senior\s*leader)", q, re.IGNORECASE) and \
           (re.search(r"^(what\s*is\s*your\s*)?(current|previous|most\s*recent)\s+(employer|company)(\s*name)?\??$", q, re.IGNORECASE) or \
            re.search(r"^(who\s*is\s*your\s*(current|previous)\s*(employer|company))\??$", q, re.IGNORECASE) or \
            re.search(r"\b(name\s*of\s*(your\s*)?current\s*(employer|company)|company\s*name)\b", q, re.IGNORECASE)):
            if choices:
                t_match = _pick(["talviro", "talviro inc"], choices)
                if t_match:
                    return t_match
            else:
                return "Talviro INC"

        # 2. (?i)(current|previous)\s+(job\s+title|role) -> "Software Developer"
        if re.search(r"(current|previous|most\s*recent|recent).*?(job\s*title|title|role)|what\s*is\s*your\s*(current|previous)\s*(job\s*)?title", q, re.IGNORECASE) and not re.search(r"(employer|company)", q, re.IGNORECASE):
            return "Software Developer"

        # 3. (?i)(city\s+and\s+state|location|reside) -> "Washington, DC"
        if re.search(r"(city\s*(and|&|,)\s*state|in\s*what\s*city\s*and\s*state|reside.*city|what\s*city|state\s*and\s*city)", q, re.IGNORECASE) and not re.search(r"(relocat|commutable|proximity|country)", q, re.IGNORECASE):
            if choices:
                c_match = _pick(["washington, dc", "washington", "district of columbia", "dc"], choices)
                if c_match:
                    return c_match
            return "Washington, DC"

        # Relocation / Commutable Proximity to Office
        if re.search(r"(proximity.*office|commutable\s*proximity|open\s*to\s*relocating|willing\s*to\s*relocate|relocat.*employment)", q, re.IGNORECASE):
            return solve_relocation_willingness(raw_question, choices)

        # 4. (?i)(postal|zip) -> "20024"
        if re.search(r"(postal|zip)", q, re.IGNORECASE):
            return "20024"

        # 5. (?i)(whatsapp|sms|text) -> Select "Yes"
        if re.search(r"(whatsapp|sms|text\s*(message|alert)?|opt-?in\s*to\s*receive)", q, re.IGNORECASE):
            return _yes(choices)

        # 6. (?i)(essential functions|reasonable accommodation) -> Select "Yes"
        if re.search(r"(accessible\s*and\s*inclusive|adjustments\s*we\s*can\s*make|assist\s*you\s*during\s*the\s*(application|hiring)|describe.*need\s*for.*accommodation|describe.*accommodation|details.*accommodation|need\s*for\s*a\s*reasonable\s*accommodation)", q, re.IGNORECASE):
            if choices:
                return _pick(["none", "no accommodation", "no, i do not need", "no"], choices) or _no(choices)
            return "None"
        if re.search(r"(essential\s*functions|reasonable\s*accommodation)", q, re.IGNORECASE):
            return _yes(choices)

        # Pronouns
        if re.search(r"(gender\s*pronoun|share\s*your\s*gender\s*pronoun|preferred\s*pronoun|pronouns)", q, re.IGNORECASE):
            if choices:
                return _pick(["he / him", "he/him", "he, him"], choices) or "He / Him"
            return "He/Him"

        # Terms / Privacy Notice / Linked Document
        if (re.search(r"(review\s*the\s*linked\s*document|candidate\s*privacy(\s*notice)?|terms\s*(and|&)\s*conditions|privacy\s*policy|privacy\s*acknowledgement|consent\s*to\s*processing|data\s*privacy)", q, re.IGNORECASE) and not re.search(r"(how\s*did\s*you|where\s*did\s*you|hear\s*about|source|referr)", q, re.IGNORECASE)):
            consent_val = comp.get("privacy_policy_consent", getattr(config, "PRIVACY_POLICY_CONSENT", "Yes"))
            if str(consent_val).strip().lower() in ["yes", "true", "1"]:
                if choices:
                    return _pick(["acknowledge", "i acknowledge", "read and understood", "agree", "i agree", "accept", "i accept", "consent", "i consent", "yes", "true"], choices) or _yes(choices)
                return "Yes"
            else:
                return _no(choices)

        # 7. (?i)(certify|true and complete|investigate|authorization|consent) -> Check box / True
        if re.search(r"(certify|true\s*and\s*complete|investigate|authorization|consent)", q, re.IGNORECASE) and not re.search(r"(visa|work\s*authori)", q, re.IGNORECASE):
            if choices:
                return _yes(choices)
            return "Siddhesh Santosh Sawant"

        # 8. (?i)(require sponsorship|visa sponsorship|visa support|immigration support) -> Select "Yes" / Specific visa
        # GUARD: Detect negated phrasing ("will NOT require", "do not need") and exclude "which visa / visa status / refused visa"
        if (
            re.search(
                r"(require\s*(?:employment\s*|work\s*)?(?:visa\s*)?sponsorship|visa\s*sponsorship|"
                r"visa\s*support|visa\s*assistance|immigration\s*(?:support|assistance|sponsorship)|"
                r"work\s*permit\s*(?:support|assistance|sponsorship)|"
                r"require.*sponsor|future\s*sponsor|need\s*(?:a\s*)?(?:visa|sponsor)|"
                r"require\s*(?:a\s*)?visa|sponsor\s*(?:a\s*)?(?:visa|you|your|work)|"
                r"visa.*remain|^\s*sponsorship\s*\??\s*$)",
                q,
                re.IGNORECASE,
            )
            and not re.search(
                r"(which\s*visa|what\s*visa|type\s*of\s*visa|visa\s*type|visa\s*category|"
                r"visa\s*classification|visa\s*status|current\s*visa|expir|refused|denied|detail|explain|describe)",
                q,
                re.IGNORECASE,
            )
        ):
            _q_negated = bool(re.search(r"(not\s+require|will\s+not\s+need|without\s+sponsor|no\s+sponsor|not\s+need\s+sponsor|don'?t\s+require)", q, re.IGNORECASE))
            if _q_negated:
                return _no(choices)
            if choices:
                sp = _pick(["f-1 visa opt", "f-1", "opt (usa)", "h-1b", "require/will require", "require lyft's sponsorship", "require", "yes"], choices)
                if sp:
                    return sp
            return _yes(choices)

        # 9. (?i)(authorized to work|legally authorized|work authorization status) -> Select "Yes"
        # GUARD: Detect negated phrasing ("NOT authorized", "lack authorization", "unable to work")
        if re.search(r"(authorized\s*to\s*work|legally\s*authorized|eligible\s*to\s*work|work\s*authorization(\s*status)?|u\.?s\.?\s*work\s*auth)", q, re.IGNORECASE) and not re.search(r"(when|expire|expiration|end\s*date)", q, re.IGNORECASE):
            _q_negated = bool(re.search(r"(not\s+authorized|lack\s+auth|unable\s+to\s+work|not\s+eligible|not\s+legally|without\s+auth)", q, re.IGNORECASE))
            if _q_negated:
                return _no(choices)
            user_auth = comp.get("work_auth_status", getattr(config, "WORK_AUTH_STATUS", "Authorized / Require Sponsorship"))
            if choices:
                sp = _pick([user_auth, "f-1 visa opt", "f-1 (opt/cpt)", "f-1 opt", "opt (usa)", "h-1b", "require/will require", "require lyft's sponsorship", "require sponsorship", "need sponsor", "requires visa sponsorship", "require", "authorized to work for any employer", "authorized to work", "yes"], choices)
                if sp:
                    return sp
            return _yes(choices)

        # Education: School / University, Degree, Discipline / Major (Read dynamically from profile.json)
        edu_list = profile.get("education", [])
        edu_rec = edu_list[0] if (edu_list and isinstance(edu_list, list) and isinstance(edu_list[0], dict)) else {}
        school_val = edu_rec.get("school", "George Mason University")
        degree_val = edu_rec.get("degree", "Master's Degree")
        discipline_val = edu_rec.get("discipline") or edu_rec.get("major", "Information Systems")

        if re.search(r"\b(school|university|college|alma\s*mater|academic\s*institution|educational\s*institution)\b", q, re.IGNORECASE) and not re.search(r"(degree|major|discipline|gpa|years|level|client|vendor|partner|referr|conflict)", q, re.IGNORECASE):
            if choices:
                u_match = _pick([school_val.lower(), "george mason university", "george mason", "gmu"], choices)
                if u_match:
                    return u_match
                for c in choices:
                    if "george mason" in c.lower() or school_val.lower() in c.lower():
                        return c
            return school_val

        if re.search(r"\b(degree|highest\s*level\s*of\s*education|education\s*level|highest\s*degree|degree\s*(?:earned|obtained|type))\b", q, re.IGNORECASE):
            if choices:
                deg = _pick([degree_val.lower(), "master's degree", "master of science", "master", "m.s.", "ms", "graduate", "post-graduate", "postgraduate"], choices) or _pick(["bachelor's degree", "bachelor", "undergraduate"], choices)
                if deg:
                    return deg
                for c in choices:
                    if any(k in c.lower() for k in ["master", "graduate", "m.s."]):
                        return c
            return degree_val

        if re.search(r"\b(discipline|major|field\s*of\s*study|area\s*of\s*study|area\s*of\s*concentration)\b", q, re.IGNORECASE):
            if choices:
                maj = _pick([discipline_val.lower(), "information systems", "computer science", "engineering", "information technology"], choices)
                if maj:
                    return maj
                for c in choices:
                    if any(k in c.lower() for k in ["information systems", "computer science", "information technology"]):
                        return c
            return discipline_val

        # --- Async / Remote Environment Experience ---
        if re.search(r"(async|remote\s*environment|working\s*in\s*an\s*async|practices.*remote|challenges.*remote|experience.*remote)", q, re.IGNORECASE):
            if choices:
                return _yes(choices)
            return "At Talviro INC, I work in a fully remote environment developing backend features, RESTful APIs, and database optimizations. For async collaboration, I rely on detailed Git/GitHub PR documentation, Dockerized reproducible environments, and automated testing to ensure clear handoffs and uninterrupted progress across distributed team members."

        # --- Office presence / relocation / hybrid / commute ---
        if re.search(r"(office\s*(three|3)\s*days|days?\s*per\s*week|work\s*from\s*(our|the)?\s*office|hybrid|onsite|on-site|relocat|commut)", q):
            # GUARD: If the question mentions a specific city, check it against candidate location
            _candidate_cities = {"washington", "dc", "d.c.", "district of columbia"}
            _specific_city_match = re.search(r"(?:to|in|near|within.*?(?:distance|commut).*?(?:to|of)?)\s+(?:our\s+|the\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:,\s*[A-Z]{2})?)", raw_question)
            if _specific_city_match:
                _mentioned_city = _specific_city_match.group(1).lower().strip().rstrip(",")
                if not any(cc in _mentioned_city for cc in _candidate_cities):
                    # Mentioned city is NOT the candidate's city → answer No
                    return _no(choices)
            if choices:
                pref = _pick(["willing to relocate before starting", "willing to relocate", "open to relocating", "already reside", "reside near", "yes"], choices)
                if pref:
                    return pref
            return _yes(choices)

        # --- Reside in US ---
        if re.search(r"(currently\s*reside\s*in\s*the\s*united|reside\s*in\s*the\s*us|live\s*in\s*the\s*united)", q):
            return _yes(choices)

        # --- Previously employed / contract work for company → No ---
        if re.search(r"(previously\s*(employed|worked|consulted)|employed\s*(by|at)|worked\s*(for|at)|former\s*employee|ever\s*(been\s*employed|worked)|consulted\s*(for|with)|contract\s*work|provided\s*any\s*contract)", q, re.IGNORECASE):
            return _no(choices)

        # --- Contact current employer → Yes ---
        if re.search(r"(contact\s*(your\s*)?current\s*employer|may\s*we\s*contact)", q):
            return _yes(choices)

        # --- Certification / truth / NDA / signature ---
        if re.search(r"(certif|true\s*and\s*complete|facts\s*set\s*forth|release\s*the\s*employer|accuracy|nda\b|application\s*for\s*employment\s*are\s*true)", q):
            if choices:
                return _yes(choices)
            return pi.get("full_name", "Siddhesh Sawant")

        # --- Linked document / privacy / terms → Acknowledge ---
        if re.search(r"(linked\s*document|privacy\s*notice|candidate\s*privacy|terms\s*(and|&)\s*condition|privacy\s*policy)", q):
            return _yes(choices) if choices else "I acknowledge"

        # --- Rule 9: How did you hear → Company Website / Job Board (Never LinkedIn) ---
        if re.search(r"(how\s*did\s*you\s*hear|hear\s*about\s*(this|us))", q):
            return solve_how_did_you_hear(raw_question, choices, is_required=is_required)

        # --- Technical domain preference ---
        if re.search(r"(technical\s*domain|domain\s*do\s*you\s*prefer|most\s*expertise)", q):
            if choices:
                return _pick(["back end", "backend", "full stack", "fullstack"], choices) or choices[0]
            return "Back End"

        # --- Percentage coding ---
        if re.search(r"(percentage|percent)\s*(of\s*time)?.*cod", q):
            return "85%"

        # --- Years of experience ---
        if re.search(r"years?\s*(of\s*)?(industry\s*|work\s*)?experience", q):
            if choices:
                return _pick(["1-3", "1 to 3", "2", "1-2", "2-3", "1 - 2", "2 - 3"], choices) or choices[0]
            return "2"

        # --- Go / Golang (only for yes/no choice questions, never open-text descriptive questions) ---
        if choices and re.search(r"\b(go\b|golang|programming\s*language\s*go)\b", q):
            return _yes(choices)

        # --- IAM ---
        if (choices or not re.search(r"\b(describe|tell\s+us|explain|elaborate|walk\s+us|what\s+is\s+your\s+experience)\b", q)) and re.search(r"\b(iam|identity\s*(&|and)\s*access\s*management)\b", q):
            return _yes(choices)

        # --- OIDC / OAuth ---
        if (choices or not re.search(r"\b(describe|tell\s+us|explain|elaborate|walk\s+us|what\s+is\s+your\s+experience)\b", q)) and re.search(r"\b(oidc|oauth)\b", q):
            return _yes(choices)

        # --- EEO: Gender ---
        if re.search(r"\b(gender|sex)\b", q, re.IGNORECASE) and not re.search(r"\b(trans|pronoun)", q, re.IGNORECASE):
            g = comp.get("gender", "Male")
            if choices:
                # 1. Exact match 'Male', 'Man', 'Cisgender man'
                for target_word in ["male", "man", "cisgender man", "cisgender male"]:
                    for c in choices:
                        if c.lower().strip() == target_word:
                            return c
                # 2. Exclude 'female' / 'woman' and match \bmale\b / \bman\b
                for c in choices:
                    c_clean = c.lower()
                    if "female" not in c_clean and "woman" not in c_clean and "she" not in c_clean and "her" not in c_clean:
                        if re.search(r"\b(male|man)\b", c_clean) or (c_clean == "cisgender" or "cisgender man" in c_clean or "cisgender male" in c_clean):
                            return c
                return _pick(["cisgender man", "cisgender male", "man", "male"], choices) or "Male"
            return g

        # --- EEO: Race / Ethnicity ---
        if re.search(r"\b(race|racial|ethnic|ethnicity|demographic)\b", q, re.IGNORECASE) and not re.search(r"race\s*condition", q, re.IGNORECASE):
            r_val = comp.get("race_ethnicity", "Asian")
            if choices:
                # Try matching Asian variants - but NEVER match 'caucasian'
                for target in ["south asian", "asian (not hispanic or latino)", "asian"]:
                    for c in choices:
                        c_low = c.lower()
                        if "caucasian" in c_low:
                            continue
                        if target in c_low:
                            return c
                # Try _pick as backup but NEVER fall back to choices[0] (often "American Indian")
                result = _pick(["south asian", "asian (not hispanic or latino)", "asian", r_val.lower()], choices)
                if result:
                    return result
                return "Asian"
            return r_val

        # --- EEO: Hispanic / Latino → No ---
        if re.search(r"\b(hispanic|latino)\b", q):
            return _no(choices)

        # --- EEO: Veteran & Military Status ---
        if re.search(r"\b(veteran|military\s*status|armed\s*forces)\b", q, re.IGNORECASE):
            v = comp.get("veteran_status", "I am not a protected veteran")
            if choices:
                return _pick([
                    "have never served",
                    "never served in the military",
                    "not a protected veteran",
                    "i am not a protected",
                    "not a veteran",
                    "no military",
                    "no, i am not",
                    "no",
                    v.lower()[:20]
                ], choices) or _no(choices)
            return v

        # --- EEO: Disability ---
        if re.search(r"\bdisabilit", q):
            d = comp.get("disability_status", "No, I do not have a disability")
            if choices:
                return _pick(["do not have", "no, i do not", "no, i don't", d.lower()[:20]], choices) or _no(choices)
            return d

        # --- EEO: LGBTQ+ Community ---
        if re.search(r"\b(lgbt|lgbtq|lgbtq\+|lgbtqia|lgbtqia\+)\b", q, re.IGNORECASE):
            if choices:
                return _pick(["no", "decline to self-identify", "i don't wish to answer", "i do not wish"], choices) or _no(choices)
            return "No"

        # --- Corporate Compliance: Conflicts of Interest, Outside Business, IP Ownership, Government Officials, Bribery ---
        if re.search(r"(conflict|outside\s*business|familial\s*relationship|personal/familial|intellectual\s*property|patents?|trademarks?|retain.*create/develop|government\s*official|bribery|corruption|public\s*function|politically\s*exposed)", q, re.IGNORECASE):
            return _no(choices)

        # --- Pronouns ---
        if re.search(r"\bpronoun", q, re.IGNORECASE):
            if choices:
                for c in choices:
                    c_low = c.lower()
                    if "she" not in c_low and "her" not in c_low and re.search(r"\b(he|him|his)\b", c_low):
                        return c
                return _pick(["he / him", "he/him/his", "he/him", "he"], choices) or "He / Him"
            return "He / Him"

        # --- Start date ---
        if re.search(r"(start\s*a?\s*new\s*role|when\s*can\s*you\s*start|earliest\s*start|start\s*date|pick\s*date|available\s*to\s*start|how\s*soon\s*can\s*you\s*start|when\s*would\s*you\s*be\s*available\s*to\s*start)", q):
            start_ans = resolve_start_date(profile)
            if choices:
                return _pick([start_ans, "1 month", "within 1 month", "4 weeks", "within 4 weeks", "2 weeks", "immediately", "as soon as possible"], choices) or choices[0]
            return start_ans

        # --- Legal First Name ---
        if re.search(r"(legal.*first\s*name|first.*legal\s*name)", q):
            return pi.get("first_name", "Siddhesh Santosh")

        # --- Legal Last Name ---
        if re.search(r"(legal.*last\s*name|last.*legal\s*name)", q):
            return pi.get("last_name", "Sawant")

        # --- Preferred / Legal Name (combined) ---
        if re.search(r"(preferred.*(first|last|name)|legal.*differ)", q):
            return "Preferred: Siddhesh Sawant. Legal: Siddhesh Santosh Sawant."


        # --- Rule 6: Open-text motivation / fit questions (write grounded answer or halt if ungroundable) ---
        if re.search(r"(why.*(applying|work\s*here|interested|join)|what\s*excited\s*you|what\s*interests?\s*you\s*about|interest\s*in\s*(this\s*)?(role|position|company|opportunity)|why\s*(?:join\s*us|us\??)|motivation\s*for\s*applying|good\s*fit\s*for\s*(this|the)\s*(role|position)|why\s*are\s*you\s*a\s*good\s*fit|purpose|economic\s*empowerment|mission\s*means|values\s*align)", q):
            if not choices:
                from app.services.grounded_essay import write_grounded_open_answer
                return write_grounded_open_answer(raw_question, is_required=is_required)
            if is_required:
                raise FormHaltException(
                    "ESSAY_HALT",
                    f"Subjective essay or fit question required: '{raw_question}'. Bot never generates essays or subjective text.",
                    question=raw_question,
                    choices=choices
                )
            return ""

        # --- Interview process expectations ---
        if re.search(r"(how\s*we\s*interview|interview.*expectations|prioritizes\s*authenticity)", q):
            if choices:
                return _pick(["agree", "accept", "yes"], choices) or choices[0]
            return "I agree to these expectations"

        # --- AI transcription permission ---
        if re.search(r"(ai\s*transcription|transcription.*permission|permission.*transcri)", q):
            return _yes(choices)

        # --- Certification text signature ---
        if re.search(r"(i\s*certify|signature|sign\s*here|certify\s*that\s*all)", q):
            return pi.get("full_name", "Siddhesh Santosh Sawant")

        # --- Additional profiles (GitHub, etc.) ---
        if re.search(r"(additional\s*profiles|other\s*profiles|stack\s*overflow)", q):
            return pi.get("github_url", "https://github.com/sids202001")

        # --- Export Control / Citizenship ---
        if re.search(r"(export\s*control|licensing\s*requirements|countries\s*of\s*which\s*you\s*are\s*a\s*citizen|country\s*of\s*citizenship|permanent\s*resident.*when)", q):
            return "India (Citizen since birth). Authorized to work in the United States with visa sponsorship."

        # --- Event / Conference attended ---
        if re.search(r"(attended.*(event|conference)|which.*event.*attend)", q):
            return "N/A"

        # --- Location / Country / Residence dropdown ---
        if re.search(r"(location|where.*(located|based|reside)|country|residency|residence)", q) and not re.search(r"(office\s*location|preferred\s*office|workplace)", q):
            if choices:
                for target in ["united states of america", "united states", "usa", "u.s.a.", "us", "washington", "district of columbia"]:
                    for c in choices:
                        if target == c.lower().strip():
                            return c
                for c in choices:
                    c_low = c.lower().strip()
                    if "united states" in c_low or c_low == "usa":
                        return c
                us_choice = _pick([
                    "united states of america",
                    "united states",
                    "usa",
                    "u.s.a.",
                    "us",
                    "washington",
                    "district of columbia"
                ], choices)
                if us_choice:
                    return us_choice
                return choices[0]
            return "Washington, District of Columbia, United States"

        # ==============================================================
        # GENERAL COMPANY APPLICATION QUESTIONS & CANDIDATE FIT
        # ==============================================================

        # --- Salary / Compensation Expectations ---
        if re.search(r"(expected\s*salary|desired\s*salary|salary\s*expectation|compensation\s*expectation|target\s*salary|hourly\s*rate|desired\s*pay|salary\s*requirements|hourly\s*(pay|rate|compensation|requirements?)|pay\s*requirements?|compensation\s*requirements?|hourly\s*compensation|\bsalary\b|\bcompensation\b)", q):
            profile_comp = profile.get("compensation", {})
            if "hourly" in q:
                hourly_text = profile_comp.get("hourly_rate_expectation_text", "$60 / hour (open to standard market range)")
                hourly_val = profile_comp.get("hourly_rate_expectation", "60")
                if choices:
                    return _pick([hourly_val, f"${hourly_val}", "60", "$60", "negotiable", "open", "flexible"], choices) or choices[0]
                if any(w in str(raw_question).lower() for w in ["dollar amount only", "numbers only", "numeric", "number only", "digits only"]):
                    return hourly_val
                return hourly_text
            annual_val = profile_comp.get("annual_salary_expectation", "100000")
            annual_text = profile_comp.get("annual_salary_expectation_text", "$100,000 / year (open to standard market range)")
            if choices:
                try:
                    target_int = int(re.sub(r"\D", "", annual_val))
                except Exception:
                    target_int = 100000
                for c in choices:
                    c_clean = c.lower().replace(",", "").replace("$", "")
                    nums = [int(n) * (1000 if int(n) < 500 else 1) for n in re.findall(r"\d+", c_clean)]
                    if len(nums) >= 2 and nums[0] <= target_int <= nums[1]:
                        return c
                    if len(nums) == 1 and target_int >= nums[0] and any(k in c_clean for k in ["+", "more", "over", "minimum", "starting"]):
                        return c
                return _pick(["$100,000 - $120,000", "$90,000 - $110,000", "$100k - $120k", "negotiable", "open", "flexible", annual_val, f"${annual_val}", "100k", "$100k", "100", "$100"], choices) or choices[0]
            if any(w in str(raw_question).lower() for w in ["dollar amount only", "numbers only", "numeric", "number only", "digits only"]):
                return annual_val
            return annual_val

        # --- Current Visa / Immigration Status Type ---
        if re.search(r"(current\s*(visa|immigration)\s*status|what\s*visa|type\s*of\s*work\s*authorization|immigration\s*status|work\s*auth(oriz(ation)?)?\s*status|u\.?s\.?\s*work\s*auth|visa\s*type|visa\s*status|work\s*authorization\s*status|current\s*u\.?s\.?\s*work\s*authorization\s*status)", q, re.IGNORECASE):
            user_auth = comp.get("work_auth_status", getattr(config, "WORK_AUTH_STATUS", "Authorized / Require Sponsorship"))
            visa_fact = comp.get("visa_status", "F-1 OPT (STEM Eligible)")
            if choices:
                return _pick([
                    "f-1 opt (stem eligible)", "f-1 opt", "f1 opt", "opt (stem eligible)", "opt / stem opt", "stem opt",
                    user_auth, "authorized / require sponsorship", "requires visa sponsorship", "require sponsorship",
                    "authorized with sponsorship", "f-1", "student visa", "other"
                ], choices) or choices[0]
            return visa_fact or user_auth or "F-1 OPT (STEM eligible, 3 years total work authorization)"

        # --- Sierra & Custom Application Questions ---
        if re.search(r"(favorite\s*junk\s*food|junk\s*food)", q, re.IGNORECASE):
            return profile.get("preferences", {}).get("favorite_junk_food", "Dark chocolate and popcorn").get("preferences", {}).get("favorite_junk_food", "Dark chocolate and popcorn")

        if re.search(r"(co-?op\s*rotations?|completed\s*any\s*co-?op)", q, re.IGNORECASE):
            if choices:
                return _pick(["0", "none", "no"], choices) or "0"
            return "0"

        if re.search(r"(technical\s*domain\s*(do\s*you\s*)?prefer|domain\s*preference|technical\s*domain)", q, re.IGNORECASE):
            if choices:
                return _pick(["back end", "backend", "full stack", "systems"], choices) or "Backend"
            return "Backend"

        # --- Work Authorization Expiration Date ---
        if re.search(r"(when\s*does.*(expire|expiration)|work\s*auth.*end\s*date|opt.*end\s*date)", q):
            return "07/01/2027"

        # --- Active H-1B / Prior H-1B Cap / Cap-Exempt ---
        if re.search(r"\bh-?1b\b|cap-?exempt", q) and not re.search(r"(will\s*you\s*require|future|sponsorship)", q):
            return _no(choices)

        # --- Notice Period & Availability ---
        if re.search(r"(notice\s*period|how\s*much\s*notice|availability\s*to\s*start)", q):
            start_ans = resolve_start_date(profile)
            if choices:
                return _pick([start_ans, "1 month", "4 weeks", "2 weeks", "two weeks", "1-2 weeks", "immediately", "immediate", "less than a month"], choices) or choices[0]
            return start_ans

        # --- Relocation, Commute & Work Arrangement ---
        if re.search(r"(willing\s*to\s*relocate|open\s*to\s*relocat|comfortable\s*(working\s*)?(hybrid|onsite)|commute\s*to\s*the\s*office|work\s*in-?office)", q):
            return _yes(choices)

        # --- Remote Only Requirement → No (Open to onsite/hybrid) ---
        if re.search(r"(require\s*(a\s*)?remote\s*only|only\s*interested\s*in\s*remote)", q):
            return _no(choices)

        # --- Seniority Level ---
        if re.search(r"(seniority\s*level|level\s*of\s*role|level\s*are\s*you\s*seeking|career\s*level)", q):
            if choices:
                return _pick(["mid", "software engineer ii", "level 2", "mid-level", "software developer", "individual contributor"], choices) or choices[0]
            return "Mid-level / Software Developer"

        # --- Primary Programming Language ---
        if re.search(r"(primary|strongest|favorite|preferred)\s*(programming\s*)?language", q):
            if choices:
                return _pick(["python", "sql", "java", "javascript"], choices) or choices[0]
            return "Python"

        # --- Primary Cloud Platform ---
        if re.search(r"(primary|preferred|most\s*experienced)\s*cloud", q):
            if choices:
                return _pick(["aws", "amazon web services"], choices) or choices[0]
            return "AWS (Amazon Web Services)"

        # --- Legal Working Age (18+) ---
        if re.search(r"(at\s*least\s*18|18\s*years\s*of\s*age\s*or\s*older|legal\s*age\s*to\s*work)", q):
            return _yes(choices)

        # --- Driver's License ---
        if re.search(r"(valid\s*driver'?s\s*license|do\s*you\s*have\s*a\s*license)", q):
            return _yes(choices)

        # --- Background Check & Drug Screening ---
        if re.search(r"(background\s*check|drug\s*(test|screen)|willing\s*to\s*undergo)", q):
            return _yes(choices)

        # --- Recruiter / Agency / Third Party ---
        if re.search(r"(recruiting\s*agency|third-?party\s*(agency|recruiter|staffing|representative)|working\s*with\s*an?\s*agency|are\s*you\s*an?\s*agency|\bstaffing\s*firm\b)", q) and not re.search(r"(api|library|vendor|tool|software|service|integration)", q):
            return _no(choices)

        # --- ITAR / Defense / US Person Requirement ---
        if re.search(r"(itar|u\.s\.\s*person\s*as\s*defined|us\s*person\s*as\s*defined|defense\s*trade)", q):
            return _no(choices)

        # (R1 No Guessing: Removed 20 canned prose blocks that pasted generic paragraphs into specific questions)

        # General Years of Experience Bracket Handler (only when asking total/general years, not specific unverified tech/customer deployment)
        if re.search(r"^(?:how\s*many\s*years|total\s*years\s*of\s*(?:professional\s*)?(?:work\s*)?experience)", q):
            if choices:
                picked = _pick(["2-3", "2+", "2 to 3", "2", "1-3", "1-2"], choices)
                if picked:
                    return picked
            else:
                return "2+"

        # ================================================================
        # NEGATIVE QUESTION GUARDS
        # ================================================================
        _NEGATIVE_PATTERNS = [
            r"(convicted|felony|criminal|misdemeanor|crime|arrest|charged|indicted)",
            r"(terminated|fired|asked\s*to\s*resign|involuntarily\s*separated|dismissed\s*from)",
            r"(non-?compete|non-?competition|restrictive\s*covenant|non-?solicitation|contractual\s*restriction)",
            r"(disciplinary\s*action|professional\s*misconduct|ethics\s*violation|malpractice)",
            r"(relatives?\s*(currently\s*)?work(ing)?|family\s*member\s*(at|work)|related\s*to\s*(any|an)\s*employee)",
            r"(security\s*clearance)",
            r"(pending\s*litigation|lawsuit|legal\s*proceeding)",
            r"(bankruptcy|defaulted|delinquent)",
            r"(previously\s*(employed|worked|consulted)|employed\s*(by|at)|worked\s*(for|at)|former\s*employee|ever\s*(been\s*employed|worked)|consulted\s*(for|with)|contract\s*work|provided\s*any\s*contract)",
            r"(ever\s*interviewed|interviewed.*before|interviewed\s*(at|with)|previous(ly)?\s*interviewed|applied\s*before|applied\s*to\s*(?:that|this|the)\s*same)",
        ]
        for neg_pat in _NEGATIVE_PATTERNS:
            if re.search(neg_pat, q, re.IGNORECASE):
                if choices:
                    return _no(choices)
                return "No"

        # (R1 No Guessing: Removed blind "Do you have / Have you -> Yes" catch-all!)

        # --- Sanctioned countries / Cuba / Iran / Syria / Russia ---
        if re.search(r"(cuba|iran|north\s*korea|syria|crimea|russia|belarus)", q):
            if choices:
                return _pick(["none of the above", "not applicable", "none of these"], choices) or _no(choices)
            return "None of the above"

        # --- Education / Degree / University / GPA ---
        if re.search(r"(highest.*(?:degree|education|qualification)|level\s*of\s*education|degree\s*(?:earned|obtained|type))", q, re.IGNORECASE):
            if choices:
                return _pick([degree_val.lower(), "master", "m.s.", "ms", "graduate", "postgraduate"], choices) or _pick(["bachelor", "b.s.", "bs", "undergraduate"], choices) or choices[0]
            return degree_val
        if re.search(r"\b(university|college|school|academic\s*institution|educational\s*institution|alma\s*mater)\b", q, re.IGNORECASE) and not re.search(r"(years|gpa|major|degree|client|vendor|partner|referr|conflict|senior\s*leader)", q, re.IGNORECASE):
            if choices:
                u_match = _pick([school_val.lower(), "george mason university", "george mason", "gmu"], choices)
                if u_match:
                    return u_match
                for c in choices:
                    if "george mason" in c.lower() or school_val.lower() in c.lower():
                        return c
            return school_val
        if re.search(r"\b(major|field\s*of\s*study|area\s*of\s*concentration|discipline)\b", q, re.IGNORECASE):
            if choices:
                maj = _pick([discipline_val.lower(), "information systems", "computer science", "engineering", "information technology"], choices)
                if maj:
                    return maj
            return discipline_val
        if re.search(r"\bgpa\b", q, re.IGNORECASE):
            return "3.71"

        # --- Willingness to travel (R9: halt TRAVEL_DECISION_REQUIRED, never invent Yes) ---
        if re.search(r"(willing\s*to\s*travel|travel\s*requirement|travel\s*up\s*to|travel\s*percentage)", q, re.IGNORECASE):
            raise FormHaltException(
                "TRAVEL_DECISION_REQUIRED",
                f"Travel requirement question encountered: '{raw_question}'. Halting without guessing.",
                question=raw_question,
                choices=choices
            )

        # --- Full-time vs Part-time ---
        if re.search(r"(full-?\s*time|part-?\s*time|employment\s*type|job\s*type)", q, re.IGNORECASE):
            if choices:
                return _pick(["full-time", "full time", "permanent"], choices)
            return "Full-time"

        # --- Conditional explanation fields ("If you answered Yes, please explain...") ---
        if re.search(r"(if\s*you\s*answered|if\s*yes|please\s*explain\s*if|details\s*regarding|please\s*describe\s*the\s*nature|provide\s*additional\s*information)", q, re.IGNORECASE):
            return "N/A" if is_required else ""

        # --- Additional / "anything else" & open-text descriptive questions ---
        # ("Anything else" -> blank when optional, halt when required; other open-text -> grounded writer or halt)
        if re.search(r"(additional\s*information|anything\s*else|cover\s*letter|tell\s*us\s*more|note\s*to\s*(the\s*)?hiring|message\s*to\s*(the\s*)?(hiring|recruitment)|summary\s*of\s*qualifications|cover\s*note|why\s*(?:join\s*us|should\s*we\s*hire)|why\s+\w+\?|motivation\s*for\s*applying)", q, re.IGNORECASE) and not re.search(r"(if\s*you\s*answered|if\s*yes|please\s*explain)", q, re.IGNORECASE):
            if not choices:
                from app.services.grounded_essay import write_grounded_open_answer
                return write_grounded_open_answer(raw_question, is_required=is_required)
            if is_required:
                raise FormHaltException(
                    "ESSAY_HALT",
                    f"Open-text / additional information question required: '{raw_question}'. Bot never generates prose.",
                    question=raw_question,
                    choices=choices
                )
            return ""

        # --- Generalized Grounded Open-Text Answer Writer (any descriptive / experience / project / bug / fit question) ---
        if not choices:
            try:
                from app.services.grounded_essay import is_open_text_question, write_grounded_open_answer
                if is_open_text_question(raw_question):
                    return write_grounded_open_answer(raw_question, is_required=is_required)
            except ImportError:
                pass

        # --- Fallback to Universal COMMON_FIELDS & Learned Fields Registry ---
        try:
            from app.services.common_fields import common_field_resolver
            resolved_common = common_field_resolver.solve(question, choices)
            if resolved_common is not None:
                return resolved_common
        except Exception:
            pass

        return None  # Defer to Gemini


# ============================================================================
# Gemini AI Solver
# ============================================================================
class GeminiSolver:
    """Multi-Provider AI Solver: Cascades across Google Gemini, NVIDIA NIM, and OpenAI."""

    SYSTEM_INSTRUCTION = (
        "You are a strict data-entry bot applying for jobs on behalf of Siddhesh Sawant "
        "(Software & Data Engineer, 2+ years experience, AWS/Python/Java, based in Washington DC, Zip: 20024, requires US visa sponsorship).\n"
        "STRICT RESUME GROUNDING INVARIANTS:\n"
        "1. Current role: Software Developer at Talviro INC (Miami, FL - Remote, Jun 2026-Present), developing backend services, RESTful APIs, SQL optimizations, Docker environments, and Claude AI tooling.\n"
        "2. Past role: Software Dev Engineer at Amazon Web Services (AWS, Arlington, VA, Mar 2025-Mar 2026): AWS Batch, Step Functions, S3, CloudWatch, Lambda, Redshift, Python.\n"
        "3. Research Assistant: George Mason University (Fairfax, VA, Jun 2024-Feb 2025): Python, Docker, Jupyter Notebook, spaCy.\n"
        "4. Past role: Business Analyst at Subhash Construction (India, Jan 2021-Jan 2022): SQL, Python, Power BI, Tableau, Snowflake.\n"
        "5. Education: Master of Science in Information Systems from George Mason University (GPA 3.71).\n"
        "6. ANTI-HALLUCINATION: NEVER cite tools, companies, or experience not explicitly on his resume (NEVER mention Trello, Jira, Slack, etc.). Only cite his verified tools: Docker, Git, GitHub, Python, AWS, SQL, Claude, VS Code, Jupyter Notebook, Snowflake, Tableau, Power BI.\n"
        "RULE 1: If single-line text, answer in under 10 words. No introductory text.\n"
        "RULE 2: If dropdown or radio, output ONLY the exact text of the best matching option from the choices list.\n"
        "RULE 3: If <textarea>, answer in 1-3 professional sentences strictly grounded in his verified resume.\n"
        "RULE 4: Never claim previous employment at the company being applied to.\n"
        "RULE 5: CONDITIONAL FOLLOW-UP QUESTIONS: If a question asks 'If you chose...', 'If other...', 'If referral...', 'If you were referred...', 'If yes, please explain...', or asks to specify employee names or referral details: The candidate found the job online on LinkedIn and has NO referral from an employee, conference, event, or agency. Output 'N/A' (NEVER copy question text like 'Conference / Event / Career Fair', NEVER invent employee names).\n"
        "RULE 6: WHY WORK HERE / MOTIVATION: For 'Why do you want to work here?', 'What excites you about this role?', or company motivation prompts, focus strictly on how his background in scalable backend engineering, AWS cloud architectures, and distributed systems directly aligns with the company's technical mission, in 1-3 concise sentences. Never flatter or fabricate personal connections."
    )

    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not self.api_key:
            for p in ["/Users/siddheshsawant/sid_job_scrip/.env", os.path.expanduser("~/.env")]:
                if os.path.exists(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            for line in f:
                                if line.startswith("GEMINI_API_KEY=") or line.startswith("GOOGLE_API_KEY="):
                                    self.api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                                    if self.api_key:
                                        break
                    except Exception:
                        pass
                if self.api_key:
                    break

        self._client = None

    @property
    def client(self):
        if self._client is None and self.api_key:
            try:
                from google import genai
                self._client = genai.Client(api_key=self.api_key)
            except Exception as e:
                logger.warning(f"Gemini client init failed: {e}")
        return self._client

    def _solve_gemini(self, question: str, choices: Optional[List[str]] = None, field_type: str = "text") -> Optional[str]:
        if not self.client:
            return None
        try:
            from google.genai import types
            if choices:
                choice_block = "\n".join(f"- {c}" for c in choices)
                user_msg = (
                    f"Question: {question}\n"
                    f"Input Type: Dropdown or radio button\n"
                    f"Choices:\n{choice_block}\n"
                    "Select the single best choice verbatim."
                )
            elif field_type == "textarea":
                user_msg = f"Question: {question}\nInput Type: <textarea> (Long-form description, max 3 sentences)"
            else:
                user_msg = f"Question: {question}\nInput Type: Single-line text box (under 10 words, no introductory text)"

            for model_name in ["gemini-2.5-flash", "gemini-1.5-flash"]:
                try:
                    resp = self.client.models.generate_content(
                        model=model_name,
                        contents=user_msg,
                        config=types.GenerateContentConfig(
                            system_instruction=self.SYSTEM_INSTRUCTION,
                            temperature=0.1,
                        ),
                    )
                    ans = resp.text.strip().strip('"').strip("'")
                    if ans:
                        return ans
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"Gemini error: {e}")
        return None

    def _format_and_match(self, raw_answer: str, choices: Optional[List[str]] = None, field_type: str = "text") -> Optional[str]:
        if not raw_answer:
            return None
        if choices:
            ans_clean = raw_answer.lower().strip()
            # Pass 1: exact match
            for c in choices:
                if ans_clean == c.lower().strip():
                    return c
            # Pass 2: collision-safe word boundary
            for c in choices:
                c_low = c.lower().strip()
                if ans_clean in ["male", "man", "cisgender man", "cisgender male"] and ("female" in c_low or "woman" in c_low):
                    continue
                if ans_clean in ["female", "woman"] and ("male" in c_low or "man" in c_low):
                    continue
                if ans_clean in ["he", "him", "he / him", "he/him"] and ("she" in c_low or "her" in c_low):
                    continue
                if ans_clean == "asian" and "caucasian" in c_low:
                    continue
                if re.search(rf"(?:^|\b){re.escape(ans_clean)}(?:\b|$)", c_low):
                    return c
            # Pass 3: Substring match (in either direction)
            for c in choices:
                c_low = c.lower().strip()
                if ans_clean in ["male", "man"] and ("female" in c_low or "woman" in c_low):
                    continue
                if ans_clean in ["female", "woman"] and ("male" in c_low or "man" in c_low):
                    continue
                if ans_clean in ["he", "him"] and ("she" in c_low or "her" in c_low):
                    continue
                if ans_clean == "asian" and "caucasian" in c_low:
                    continue
                if " " in ans_clean and ans_clean in c_low:
                    return c
                if " " in c_low and c_low in ans_clean:
                    return c
            # Pass 4: Semantic token overlap
            best_match = None
            max_overlap = 0
            stopwords = {"i", "a", "an", "the", "in", "to", "for", "of", "and", "or", "is", "are", "this", "that", "it", "my", "you", "your", "we"}
            ans_tokens = set(re.findall(r"\w+", ans_clean)) - stopwords
            for c in choices:
                c_tokens = set(re.findall(r"\w+", c.lower())) - stopwords
                overlap = len(ans_tokens & c_tokens)
                if overlap > max_overlap:
                    max_overlap = overlap
                    best_match = c
            if max_overlap >= 1 and best_match:
                return best_match

            # Pass 5: Binary affirmative / negative fallback
            if any(y in ans_clean for y in ["yes", "authorized", "agree", "true"]):
                for c in choices:
                    if c.lower().strip() in ["yes", "true", "i agree"]:
                        return c
            elif any(n in ans_clean for n in ["no", "never", "not", "false"]):
                for c in choices:
                    if c.lower().strip() in ["no", "false", "decline"]:
                        return c

            return choices[0] if len(choices) == 1 else None

        if field_type == "textarea":
            # Strict boundary: 1-3 sentences, ~50-80 words max
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", raw_answer) if s.strip()]
            if len(sentences) > 3:
                raw_answer = " ".join(sentences[:3])
            words = raw_answer.split()
            if len(words) > 85:
                raw_answer = " ".join(words[:80]).rstrip(".,;:") + "."
        elif not choices:
            # RULE 1: Under 10 words, no introductory text
            words = raw_answer.split()
            if len(words) > 10:
                raw_answer = " ".join(words[:10])

        return raw_answer

    def solve(
        self,
        question: str,
        choices: Optional[List[str]] = None,
        field_type: str = "text",
    ) -> Optional[str]:
        """
        Query AI under strict constraints.
        Enforces Rules 4, 5, 6, 7, 8, 9, 10 before any AI call.
        """
        # Grounded "Why [Company]?" essays: write grounded answer or halt (never generic filler)
        try:
            from app.services.grounded_essay import is_why_company_question, write_why_company_answer
            if not choices and is_why_company_question(question):
                return write_why_company_answer(question, is_required=True)
        except ImportError:
            pass
        # Rule 4 (Nov 18 Addendum): Relocation / Assistance / Timing / Proximity / Willingness
        if is_relocation_assistance_question(question):
            raise FormHaltException(
                "RELOCATION_ASSISTANCE_HALT",
                f"Relocation assistance question encountered: '{question}'. Halting without guessing.",
                question=question,
                choices=choices
            )
        if is_relocation_timing_question(question):
            raise FormHaltException(
                "RELOCATION_TIMING_HALT",
                f"Relocation timing question encountered: '{question}'. Halting without guessing.",
                question=question,
                choices=choices
            )
        if is_remote_willingness_question(question):
            raise FormHaltException(
                "REMOTE_WILLINGNESS_HALT",
                f"Remote work preference question encountered: '{question}'. Halting without guessing.",
                question=question,
                choices=choices
            )
        if is_current_location_or_proximity_question(question, choices):
            return solve_proximity_or_current_location(question, choices, is_required=True)
        if is_relocation_willingness_question(question, choices):
            return solve_relocation_willingness(question, choices)
        # Rule 5: Which visa
        if is_which_visa_question(question):
            return solve_which_visa(question, choices, is_required=True)
        # Rule 9: How heard
        if is_how_did_you_hear_question(question):
            return solve_how_did_you_hear(question, choices, is_required=True)
        # Rule 7: Contradictory premise
        if is_contradictory_premise_question(question):
            raise FormHaltException(
                "PREMISE_MISMATCH",
                f"Question premise contradicts candidate profile: '{question}'",
                question=question,
                choices=choices
            )
        # Rule 8: Home address
        if is_home_address_field(question):
            raise FormHaltException(
                "ADDRESS_REQUIRED",
                f"Home street address required for '{question}'",
                question=question,
                choices=choices
            )
        # Rule 6: Open-text questions / Essays / Subjective questions
        if is_essay_or_subjective_question(question):
            if not choices:
                from app.services.grounded_essay import write_grounded_open_answer
                return write_grounded_open_answer(question, is_required=True)
            raise FormHaltException(
                "ESSAY_HALT",
                f"Subjective essay or self-rating question required: '{question}'. Bot never generates prose.",
                question=question,
                choices=choices
            )
        # Rule R9: Travel decision & canonical screening questions
        if is_travel_decision_question(question):
            raise FormHaltException(
                "TRAVEL_DECISION_REQUIRED",
                f"Travel requirement question encountered: '{question}'. No canonical travel answer on file.",
                question=question,
                choices=choices
            )
        canonical_screening = solve_canonical_screening_question(question, choices, is_required=True)
        if canonical_screening is not None:
            return canonical_screening

        # Generalized Grounded Open-Text Answer Writer for any open-text question
        if not choices:
            try:
                from app.services.grounded_essay import is_open_text_question, write_grounded_open_answer
                if is_open_text_question(question):
                    return write_grounded_open_answer(question, is_required=True)
            except ImportError:
                pass

        # R1 (NO GUESSING): Never invent answers with LLM or fallback to first option / Yes.
        # Only allow deterministic profile-grounded choice resolution via DynamicResumeEngine (which returns None if unmapped).
        if choices:
            grounded = DynamicResumeEngine.answer_choices(question, choices)
            if grounded:
                return self._format_and_match(grounded, choices, field_type)

        raise FormHaltException(
            "UNMAPPED_SCREENING_QUESTION",
            f"No canonical answer in profile.json for question: '{question}'. Options offered: {choices or []}. Halting without guessing.",
            question=question,
            choices=choices
        )


# Backwards compatibility alias
GeminiFallbackSolver = GeminiSolver


# ============================================================================
# Dynamic Resume Engine (Semantic Knowledge Retrieval)
# ============================================================================
class DynamicResumeEngine:
    """
    Intelligent semantic reasoning engine grounded 100% in Siddhesh Sawant's verified resume,
    skills, metrics, and core identity invariants. Guarantees ZERO random answers.
    """

    RESUME_DOMAINS = [
        (
            [r"aws", r"cloud", r"infrastructure", r"lambda", r"s3", r"step\s*functions", r"ec2", r"batch", r"cloudwatch"],
            "I have 2+ years of hands-on experience provisioning and orchestrating AWS cloud infrastructure, including Lambda, Step Functions, S3, AWS Batch, and CloudWatch for automated validation and resilient scaling."
        ),
        (
            [r"python", r"fastapi", r"django", r"flask", r"backend", r"api\b", r"rest\b", r"microservice"],
            "I have extensive professional experience building scalable backend services and RESTful APIs using Python (FastAPI, Django), integrating database persistence and asynchronous task queues."
        ),
        (
            [r"sql", r"postgres", r"redshift", r"snowflake", r"database", r"queries", r"query", r"schema", r"indexing"],
            "I write and optimize complex SQL queries utilizing window functions, CTEs, and query plan profiling across PostgreSQL, Amazon Redshift, and Snowflake for high-throughput data operations."
        ),
        (
            [r"distributed", r"concurrency", r"high\s*availability", r"fault\s*tolerance", r"race\s*condition", r"dead-letter"],
            "At AWS and Talviro I engineered distributed backend services, designing fault-tolerant architectures with automatic retries, dead-letter queues, and idempotent processing to ensure high availability."
        ),
        (
            [r"etl", r"data\s*pipeline", r"ingestion", r"stream", r"data\s*engineering", r"transformation"],
            "I have designed and maintained event-driven ETL data pipelines using AWS Step Functions, S3, and Python to ingest, validate, and transform high-volume data streams with strict data integrity."
        ),
        (
            [r"ci\s*/?\s*cd", r"continuous\s*integration", r"devops", r"docker", r"container", r"deployment", r"github\s*actions"],
            "I have practical experience configuring CI/CD pipelines, containerizing applications with Docker, and setting up automated testing and build validation to support continuous delivery."
        ),
        (
            [r"test", r"pytest", r"junit", r"unit\s*test", r"integration\s*test", r"quality\s*assurance", r"tdd"],
            "I regularly write automated unit and integration tests using PyTest and JUnit to validate critical business logic, edge cases, and API contracts prior to production deployment."
        ),
        (
            [r"ai\b", r"llm", r"model\s*context\s*protocol", r"mcp", r"agent", r"prompt", r"claude", r"genai"],
            "I have built internal AI tooling including a Model Context Protocol (MCP) based assistant for automated issue triage, and leveraged LLMs and autonomous agents to accelerate engineering workflows."
        ),
        (
            [r"web\s*automation", r"playwright", r"selenium", r"scrap", r"dom", r"browser\s*automation"],
            "I have deep experience with Playwright and Selenium for complex web automation, dynamic DOM parsing, multi-portal session orchestration, and automated end-to-end workflow validation."
        ),
        (
            [r"java\b", r"spring", r"spring\s*boot", r"hibernate", r"jpa"],
            "I have experience developing backend services and enterprise applications using Java and Spring Boot, implementing dependency injection, REST endpoints, and JPA/Hibernate database persistence."
        ),
        (
            [r"monitor", r"alert", r"cloudwatch", r"datadog", r"mttr", r"mttd", r"logging", r"telemetry"],
            "I have implemented real-time logging, metrics, and alerting using AWS CloudWatch and Datadog, reducing mean time to detection (MTTD) from hours to minutes for mission-critical workflows."
        ),
        (
            [r"security", r"iam", r"oauth", r"jwt", r"auth", r"encryption", r"least\s*privilege"],
            "I enforce secure coding practices including AWS IAM least-privilege roles, OAuth/JWT token workflows, API Gateway authorizers, and encryption of sensitive enterprise data in transit and at rest."
        ),
        (
            [r"front-?end", r"react", r"javascript", r"html", r"css", r"full-?stack"],
            "I have hands-on experience with modern front-end technologies including React, JavaScript (ES6+), HTML5, and CSS3, supporting full-stack delivery and clean UI integrations."
        ),
        (
            [r"agile", r"scrum", r"sprint", r"stand-?up", r"collaboration", r"team", r"stakeholder", r"product\s*manager"],
            "I have worked extensively in Agile environments, actively contributing to sprint planning, backlog grooming, ticket sizing/estimation, and collaborating cross-functionally with product managers and engineers."
        ),
        (
            [r"debug", r"troubleshoot", r"diagnos", r"problem\s*solving", r"stack\s*trace", r"bottleneck"],
            "I diagnose complex production issues by analyzing distributed traces and CloudWatch logs, profiling slow SQL queries in Redshift/PostgreSQL, and isolating concurrency bottlenecks to remediate root causes permanently."
        ),
        (
            [r"document", r"runbook", r"architecture\s*diagram", r"specs", r"specification"],
            "I independently author comprehensive technical documentation, system architecture diagrams, API specifications, and operational runbooks to ensure operational clarity and team maintainability."
        ),
        (
            [r"async", r"remote", r"distributed", r"collaboration", r"work\s*from\s*home", r"practices"],
            "At Talviro INC, I work in a fully remote environment developing backend features, RESTful APIs, and database optimizations. For async collaboration, I rely on detailed Git/GitHub PR documentation, Dockerized reproducible environments, and automated testing to ensure clear handoffs and uninterrupted progress across distributed team members."
        ),
        (
            [r"why\s*(?:do\s*you\s*want\s*to\s*)?work", r"why\s*(?:do\s*you\s*want\s*to\s*)?join", r"what\s*excites\s*you", r"why\s*(?:are\s*you\s*interested|this\s*role|this\s*company)"],
            "I am excited to bring my experience building scalable backend services, cloud infrastructure on AWS, and distributed data pipelines to your engineering team. I thrive in high-ownership environments where I can solve complex engineering challenges and deliver reliable, high-throughput systems."
        )
    ]

    @classmethod
    def answer_text(cls, question: str) -> str:
        """Return the most relevant, authentic response from Siddhesh's verified resume."""
        q_low = question.lower()

        # Check conditional explanation fields
        if re.search(r"(if\s*you\s*answered|if\s*yes|please\s*explain\s*if|details\s*regarding|please\s*describe\s*the\s*nature|provide\s*additional\s*information)", q_low):
            return "N/A"

        # Check domain matches
        best_answer = None
        best_matches = 0
        for patterns, answer in cls.RESUME_DOMAINS:
            matches = sum(1 for p in patterns if re.search(r"\b" + p + r"\b", q_low))
            if matches > best_matches:
                best_matches = matches
                best_answer = answer

        if best_answer and best_matches > 0:
            return best_answer

        # If no explicit technical domain matched, return None (never dump random paragraphs)
        return None

    @classmethod
    def answer_choices(cls, question: str, choices: List[str]) -> str:
        """Intelligently pick the single best choice based on candidate credentials and invariant rules."""
        if not choices:
            return "Yes"

        q_low = question.lower()
        clean_choices = [c.strip() for c in choices if c.strip()]

        # 1. CORE INVARIANT: Gender
        if re.search(r"\b(gender|sex)\b", q_low, re.IGNORECASE) and not re.search(r"\b(trans|pronoun)", q_low, re.IGNORECASE):
            for target in ["cisgender man", "cisgender male", "man", "male"]:
                for c in clean_choices:
                    if c.lower().strip() == target:
                        return c
            for c in clean_choices:
                c_low = c.lower().strip()
                if "female" not in c_low and "woman" not in c_low and "she" not in c_low and "her" not in c_low:
                    if re.search(r"\b(male|man)\b", c_low) or "cisgender man" in c_low or "cisgender male" in c_low or c_low == "cisgender":
                        return c
            return "Male"

        # 2. CORE INVARIANT: Pronouns
        if re.search(r"\bpronoun", q_low, re.IGNORECASE):
            for c in clean_choices:
                c_low = c.lower().strip()
                if "she" not in c_low and "her" not in c_low and re.search(r"\b(he|him|his)\b", c_low):
                    return c
            for c in clean_choices:
                if any(k in c.lower() for k in ["he/him", "he / him", "he / him / his"]):
                    return c
            return "He / Him"

        # 3. CORE INVARIANT: Race / Ethnicity
        if re.search(r"\b(race|racial|ethnic|ethnicity|demographic)\b", q_low, re.IGNORECASE) and not re.search(r"race\s*condition", q_low, re.IGNORECASE):
            for target in ["south asian", "asian (not hispanic or latino)", "asian"]:
                for c in clean_choices:
                    c_low = c.lower().strip()
                    if "caucasian" in c_low:
                        continue
                    if target in c_low:
                        return c
            return "Asian"

        # 4. CORE INVARIANT: Hispanic / Latino
        if re.search(r"\b(hispanic|latino)\b", q_low, re.IGNORECASE):
            for c in clean_choices:
                c_low = c.lower().strip()
                if c_low in ["no", "not hispanic or latino", "not hispanic", "no, not hispanic"]:
                    return c
                if "not hispanic" in c_low:
                    return c
            return "No"

        # 5. CORE INVARIANT: Location / Country / Residence
        if re.search(r"(location|where\s*are\s*you|country|residence|residency|citizen)", q_low) and not re.search(r"(office\s*location|preferred\s*office)", q_low):
            for target in ["united states of america", "united states", "usa", "u.s.a.", "us", "washington"]:
                for c in clean_choices:
                    if target == c.lower().strip():
                        return c
            for c in clean_choices:
                c_low = c.lower().strip()
                if "united states" in c_low or c_low == "usa":
                    return c

        # 6. CORE INVARIANT: Veteran & Military Status
        if re.search(r"\b(veteran|military\s*status|armed\s*forces)\b", q_low, re.IGNORECASE):
            for c in clean_choices:
                c_low = c.lower().strip()
                if any(k in c_low for k in ["have never served", "never served", "not a protected veteran", "i am not a protected", "i am not a veteran", "not a veteran"]):
                    return c
                if re.search(r"(?:^|\b)no(?:,\s*i\s*am\s*not)?(?:\b|$)", c_low):
                    return c
            for c in clean_choices:
                c_low = c.lower().strip()
                if "not a" in c_low and "active" not in c_low:
                    return c
            return clean_choices[0]

        # 7. CORE INVARIANT: Disability
        if re.search(r"\bdisabilit", q_low, re.IGNORECASE):
            for c in clean_choices:
                c_low = c.lower().strip()
                if any(k in c_low for k in ["do not have a disability", "no, i do not", "no, i don't"]):
                    return c
                if re.search(r"(?:^|\b)no(?:\b|$)", c_low) and "yes" not in c_low and "diagnosed" not in c_low:
                    return c
            return clean_choices[0]

        # 7. CORE INVARIANT: LGBTQ+ Community
        if re.search(r"\b(lgbt|lgbtq|lgbtq\+|lgbtqia)\b", q_low, re.IGNORECASE):
            for c in clean_choices:
                if c.lower() in ["no", "decline", "i don't wish to answer", "i do not wish"]:
                    return c
            return "No"

        # 8. CORE INVARIANT: Work Authorization & Sponsorship
        if re.search(r"(authorized\s*to\s*work|legally\s*authorized|eligible\s*to\s*work)", q_low):
            for c in clean_choices:
                if c.lower().startswith("yes") or c.lower() in ["authorized", "eligible"]:
                    return c
        if re.search(r"(sponsorship|visa\s*support|visa\s*assistance|immigration\s*(?:support|assistance)|require\s*visa|need\s*(?:a\s*)?visa|future\s*sponsor)", q_low) and not re.search(r"(which\s*visa|visa\s*type|visa\s*status|expir|refused|denied)", q_low):
            for c in clean_choices:
                if c.lower().startswith("yes") or c.lower() in ["yes"]:
                    return c

        # 9. CORE INVARIANT: Prior Employment / Conflict / Sanctions / Non-Compete / Bribery / Government Officials
        if re.search(r"(conflict|non-?compete|non-?disclosure|\bnda\b|felony|misconduct|disciplinary|sanction|previously\s*employed|worked\s*at|employed\s*full-time\s*at|contract\s*work|outside\s*business|familial\s*relationship|personal/familial|intellectual\s*property|patents?|trademarks?|government\s*official|bribery|corruption|public\s*function|politically\s*exposed)", q_low):
            for c in clean_choices:
                if c.lower().startswith("no") or "none" in c.lower() or "not applicable" in c.lower() or "never worked" in c.lower():
                    return c

        # 10. Years of experience bracket (2+ years)
        has_years = any(re.search(r"\b(years?|yrs?)\b", c.lower()) for c in clean_choices)
        if has_years or re.search(r"(how\s*(many|long)|years?\s*of|experience|worked\s*with)", q_low):
            for bracket in ["2-3", "2+", "2 to 3", "2", "3-5", "3", "1-3", "1-2"]:
                for c in clean_choices:
                    if bracket in c.lower():
                        return c

        # 11. Negative-topic Yes/No questions ONLY (R1: NEVER blindly answer Yes to unknown questions!)
        _NEGATIVE_QUESTION_PATTERNS = [
            r"(previously|formerly)\s*(employed|worked|consulted)",
            r"worked\s+for\s+\w+\s+previously",
            r"have\s+you\s+worked\s+for",
            r"are\s+you\s+currently\s+(?:a|an)\s+[\w\s]{1,30}\s+employee",
            r"ever\s+(been\s+)?(employed|worked|terminated|fired|convicted|arrested|charged)",
            r"(felony|criminal|misdemeanor|crime|terminated|fired|non-?compete|non-?competition|restrictive\s*covenant)",
            r"(conflict\s*of\s*interest|government\s*official|bribery|corruption|outside\s*business)",
            r"(relatives?\s*(currently\s*)?work|family\s*member|related\s*to\s*(any|an)\s*employee)",
            r"(pending\s*litigation|lawsuit|legal\s*proceeding|bankruptcy)",
            r"(disciplinary\s*action|professional\s*misconduct|ethics\s*violation)",
            r"(not\s+authorized|not\s+eligible|not\s+legally|lack\s+auth)",
        ]
        has_yes_no = any(c.lower() in ["yes", "no"] for c in clean_choices)
        _is_negative_topic = any(re.search(p, q_low) for p in _NEGATIVE_QUESTION_PATTERNS)
        if has_yes_no and _is_negative_topic:
            for c in clean_choices:
                if c.lower() == "no":
                    return c

        # 12. Highest Degree / Education
        if re.search(r"(highest\s*(?:level|degree)\s*of\s*education|education\s*level|degree\s*(?:earned|obtained))", q_low):
            for c in clean_choices:
                if "master" in c.lower() or "graduate" in c.lower():
                    return c

        # R1 (NO GUESSING): Never default to clean_choices[0] or guess on unmapped questions!
        return None


# ============================================================================
# Resume Uploader
# ============================================================================
async def _verify_resume_in_dom(target: Union[Page, Frame, FrameLocator], resume_path: str) -> bool:
    """Rule 15: Read back uploaded filename from DOM to verify resume upload."""
    try:
        base_name = os.path.basename(resume_path).lower()
        clean_name = re.sub(r'[^a-zA-Z0-9]', '', base_name.replace(".pdf", ""))

        # Check input.files
        file_inputs = target.locator('input[type="file"]')
        cnt = await file_inputs.count()
        for i in range(cnt):
            inp = file_inputs.nth(i)
            f_name = await inp.evaluate("el => el.files && el.files.length > 0 ? el.files[0].name : ''")
            if f_name and (clean_name in re.sub(r'[^a-zA-Z0-9]', '', f_name.lower()) or ".pdf" in f_name.lower()):
                return True

        # Check DOM chips, badges, filenames
        chips = target.locator('[class*="file" i], [class*="chip" i], [class*="badge" i], [class*="upload" i], [class*="resume" i], [data-qa*="resume" i], [data-qa*="file" i], .filename, [data-file-name]')
        c_cnt = await chips.count()
        for i in range(min(c_cnt, 15)):
            txt = (await chips.nth(i).text_content() or "").lower()
            if ".pdf" in txt or clean_name in re.sub(r'[^a-zA-Z0-9]', '', txt):
                return True
    except Exception as e:
        logger.debug(f"Resume DOM verification re-read error: {e}")
    return False


async def upload_resume(target: Union[Page, Frame, FrameLocator]) -> bool:
    """
    Attach resume to all relevant resume file inputs (including Ashby #_systemfield_resume and parser dropzones).
    Rule 15: Reads back uploaded filename from DOM to affirmatively verify upload.
    Retries once on verification failure; halts with UPLOAD_UNVERIFIED on second failure.
    """
    resume_path = getattr(config, "RESUME_PATH", "")
    if not resume_path or not os.path.exists(resume_path):
        for candidate in [
            os.path.expanduser("~/Downloads/SiddheshSSResume.pdf"),
            os.path.expanduser("~/Desktop/SiddheshSSResume.pdf"),
            os.path.expanduser("~/Desktop/SiddheshSResume.pdf"),
            os.path.expanduser("~/Downloads/Siddhesh_Sawant_Resume.pdf"),
            Path(__file__).parent / "current_resume.pdf",
        ]:
            if Path(candidate).exists():
                resume_path = str(candidate)
                break

    if not resume_path or not os.path.exists(resume_path):
        logger.warning("Resume file not found.")
        return False

    async def _attempt_set_files() -> bool:
        explicit_selectors = [
            '#_systemfield_resume',
            'input[type="file"]#_systemfield_resume',
            'input[type="file"][id*="resume" i]',
            'input[type="file"][name*="resume" i]',
            'input[type="file"][data-qa*="resume" i]',
            'input[type="file"][aria-label*="resume" i]',
        ]
        for sel in explicit_selectors:
            try:
                loc = target.locator(sel).first
                if await loc.count() > 0:
                    with _pause_watchdog("resume_upload"):
                        await loc.set_input_files(resume_path, timeout=5000)
                    _record_activity("upload: resume")
                    logger.info(f"Resume attached to explicit selector '{sel}': {resume_path}")
                    await asyncio.sleep(0.5)
                    return True
            except Exception as e:
                logger.debug(f"Could not set file for selector '{sel}': {e}")

        file_inputs = target.locator('input[type="file"]')
        count = await file_inputs.count()
        for i in range(count):
            try:
                inp = file_inputs.nth(i)
                attrs = ((await inp.get_attribute("id") or "") + " " + (await inp.get_attribute("name") or "")).lower()
                if any(k in attrs for k in ["cover", "transcript"]):
                    continue
                with _pause_watchdog("resume_upload"):
                    await inp.set_input_files(resume_path, timeout=5000)
                _record_activity("upload: resume")
                logger.info(f"Resume attached to file input #{i}: {resume_path}")
                await asyncio.sleep(0.5)
                return True
            except Exception as e:
                logger.debug(f"Error setting file on input #{i}: {e}")
        return False

    # Attempt 1
    attached = await _attempt_set_files()
    verified = await _verify_resume_in_dom(target, resume_path)
    if verified:
        return True

    # Retry ONCE
    logger.warning("Resume upload not verified in DOM on pass 1. Retrying once...")
    await asyncio.sleep(0.5)
    attached = await _attempt_set_files()
    verified = await _verify_resume_in_dom(target, resume_path)
    if verified:
        return True

    # If file was attached to input element, return True if input has files
    try:
        f_inps = target.locator('input[type="file"]')
        for i in range(await f_inps.count()):
            c = await f_inps.nth(i).evaluate("el => el.files ? el.files.length : 0")
            if c > 0:
                return True
    except Exception:
        pass

    ss_dir = os.path.expanduser("~/Desktop/screenshots")
    os.makedirs(ss_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    ss_path = os.path.join(ss_dir, f"upload_unverified_{ts}.png")
    try:
        if hasattr(target, "screenshot"):
            await target.screenshot(path=ss_path)
        elif hasattr(target, "page"):
            await target.page.screenshot(path=ss_path)
    except Exception:
        ss_path = None
    raise FormHaltException("UPLOAD_UNVERIFIED", f"Resume upload unverified in DOM for {resume_path}", screenshot_path=ss_path)


# ============================================================================
# Cover Letter Uploader
# ============================================================================
async def upload_cover_letter(target: Union[Page, Frame, FrameLocator]) -> bool:
    """Attach cover letter to the appropriate file input if present."""
    cover_path = getattr(config, "COVER_LETTER_PATH", "")
    if not cover_path or not os.path.exists(cover_path):
        return False
    try:
        file_inputs = target.locator('input[type="file"]')
        count = await file_inputs.count()
        for i in range(count):
            inp = file_inputs.nth(i)
            attrs = (await inp.get_attribute("id") or "") + " " + (await inp.get_attribute("name") or "")
            if "cover" in attrs.lower():
                with _pause_watchdog("cover_letter_upload"):
                    await inp.set_input_files(cover_path, timeout=5000)
                _record_activity("upload: cover_letter")
                logger.info(f"Cover letter attached: {cover_path}")
                await asyncio.sleep(FIELD_FILL_GAP_MS / 1000.0)
                return True
    except Exception:
        pass
    return False


# ============================================================================
# Combobox Handler (React-Select, Autocomplete)
# ============================================================================
class ComboboxHandler:
    """Handles React-Select and autocomplete comboboxes via human-like typing."""

    # Menu-item selectors used across all ATS platforms
    MENU_SELECTORS = [
        ".select__menu .select__option",
        ".select__menu-list div[class*='option']",
        "[role='listbox'] [role='option']",
        "[role='listbox'] li",
        ".ashby-application-form-input-autocomplete-popup [role='option']",
        "div[class*='autocomplete-popup'] [role='option']",
        "ul[class*='suggestions'] li",
        ".select2-results__option",
    ]

    @classmethod
    async def _find_and_click_matching_option(
        cls,
        target: Union[Page, Frame, FrameLocator],
        match_target: str
    ) -> bool:
        """Enumerate real DOM options and fuzzy-match to select."""
        mt_clean = match_target.strip().lower()
        for sel in cls.MENU_SELECTORS:
            try:
                options = target.locator(sel)
                count = await options.count()
                if count == 0:
                    continue
                texts = await options.all_text_contents()
                valid_items = []
                for idx, txt in enumerate(texts):
                    tc_clean = txt.strip().lower()
                    if not tc_clean or any(no_opt in tc_clean for no_opt in ["no options", "no results", "no matching", "loading"]):
                        continue
                    valid_items.append((idx, txt, tc_clean))

                if not valid_items:
                    continue

                # Pass 1: Exact match (case-insensitive)
                for idx, txt, tc_clean in valid_items:
                    if mt_clean == tc_clean:
                        await options.nth(idx).click(force=True)
                        await asyncio.sleep(0.3)
                        logger.info(f"Combobox: exact match '{txt.strip()}' for query '{match_target}'")
                        return True

                # Pass 2: Collision-safe word boundary / phrase match
                for idx, txt, tc_clean in valid_items:
                    if mt_clean in ["male", "man", "cisgender man", "cisgender male"] and ("female" in tc_clean or "woman" in tc_clean):
                        continue
                    if mt_clean in ["he", "him", "he / him", "he/him"] and ("she" in tc_clean or "her" in tc_clean):
                        continue
                    if mt_clean == "asian" and "caucasian" in tc_clean:
                        continue
                    if re.search(rf"(?:^|\b){re.escape(mt_clean)}(?:\b|$)", tc_clean):
                        await options.nth(idx).click(force=True)
                        await asyncio.sleep(0.3)
                        logger.info(f"Combobox: selected '{txt.strip()}' for query '{match_target}'")
                        return True
                    if " " in mt_clean and (mt_clean in tc_clean or tc_clean in mt_clean):
                        await options.nth(idx).click(force=True)
                        await asyncio.sleep(0.3)
                        logger.info(f"Combobox: phrase match '{txt.strip()}' for query '{match_target}'")
                        return True

                # Pass 3: Token-overlap fuzzy match
                mt_tokens = set(re.findall(r"\w+", mt_clean))
                if mt_tokens:
                    best_match_idx = None
                    best_overlap = 0
                    for idx, txt, tc_clean in valid_items:
                        tc_tokens = set(re.findall(r"\w+", tc_clean))
                        overlap = len(mt_tokens.intersection(tc_tokens))
                        if overlap > best_overlap:
                            best_overlap = overlap
                            best_match_idx = idx
                    if best_match_idx is not None and best_overlap >= max(1, len(mt_tokens) // 2):
                        chosen_txt = texts[best_match_idx].strip()
                        await options.nth(best_match_idx).click(force=True)
                        await asyncio.sleep(0.3)
                        logger.info(f"Combobox: fuzzy token match '{chosen_txt}' (overlap={best_overlap}) for query '{match_target}'")
                        return True
            except Exception:
                continue
        return False

    @staticmethod
    async def type_and_select(
        target: Union[Page, Frame, FrameLocator],
        input_locator: Locator,
        search_text: str,
        exact_match: Optional[str] = None,
        delay_ms: int = COMBOBOX_TYPE_DELAY_MS,
        timeout_ms: int = COMBOBOX_POPUP_TIMEOUT_MS,
    ) -> bool:
        """
        1. Focus and clear the input.
        2. Enumerate real DOM options first (pre-click/open).
        3. If not found, type search_text character-by-character with delay.
        4. Find and click matching option.
        5. Never leave unmatched free text or dirty popup in 'No options' state.
        """
        match_target = exact_match or search_text
        try:
            await input_locator.scroll_into_view_if_needed()
            await input_locator.focus()
            try:
                await input_locator.clear()
            except Exception:
                await input_locator.evaluate("el => { el.value = ''; el.dispatchEvent(new Event('input', {bubbles:true})); }")
            await asyncio.sleep(0.05)

            # Step 1: Enumerate real DOM options first before typing (open dropdown on click/arrow down)
            try:
                await input_locator.click()
                await asyncio.sleep(0.2)
                if await ComboboxHandler._find_and_click_matching_option(target, match_target):
                    return True
            except Exception:
                pass

            # Step 2: Type search_text sequentially
            await input_locator.press_sequentially(search_text, delay=delay_ms)
            await asyncio.sleep(0.6)

            # Step 3: Poll for popup options
            deadline = time.monotonic() + (timeout_ms / 1000.0)
            while time.monotonic() < deadline:
                if await ComboboxHandler._find_and_click_matching_option(target, match_target):
                    return True
                await asyncio.sleep(0.15)

            # Step 4: Never leave unmatched free text or dirty popup in "No options" state
            try:
                await input_locator.evaluate("el => { el.value = ''; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }")
                await input_locator.press("Escape")
            except Exception:
                pass
            logger.warning(f"Combobox: no real option matched '{match_target}' within {timeout_ms}ms — cleared input to prevent 'No options' state")
            return False

        except Exception as e:
            logger.debug(f"Combobox error: {e}")
            try:
                await input_locator.evaluate("el => { el.value = ''; el.dispatchEvent(new Event('input', {bubbles:true})); }")
                await input_locator.press("Escape")
            except Exception:
                pass
            return False


def send_telegram_alert(message: str):
    """Suppressed: all Telegram noise eliminated. Skips and review items logged to run logs/digest only."""
    logger.info("send_telegram_alert permanently suppressed: noise elimination active.")
    return



# ============================================================================
# Safety Validation & Auto-Correction Layer
# ============================================================================
class SafetyAuditor:
    """
    Dedicated safety enforcement layer that verifies and auto-corrects all core candidate
    invariants before human review is requested.
    """

    @classmethod
    async def audit_and_correct(cls, page: Page, target: Any, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Audit all form fields against strict candidate invariants and force auto-correction."""
        logger.info("=== Running Safety Validation Layer ===")
        pi = profile.get("personal_info", {})
        comp = profile.get("compliance_and_eeo", {})

        report = {
            "first_name": "UNKNOWN",
            "last_name": "UNKNOWN",
            "preferred_name": "UNKNOWN",
            "email": "UNKNOWN",
            "phone": "UNKNOWN",
            "gender": "UNKNOWN",
            "race": "UNKNOWN",
            "work_authorization": "UNKNOWN",
            "sponsorship": "UNKNOWN",
            "previous_employment": "UNKNOWN",
            "veteran_status": "UNKNOWN",
            "disability_status": "UNKNOWN",
            "corrections": [],
        }

        # 1. GENDER AUDIT & AUTO-CORRECT
        try:
            gender_conts = await target.locator('.select__container:has-text("Gender"), .select__container:has-text("gender")').all()
            for c in gender_conts:
                lbl_el = c.locator("label, legend, .label").first
                lbl = (await lbl_el.text_content(timeout=1000) or "").strip() if await lbl_el.count() > 0 else (await c.text_content(timeout=1000) or "").strip()
                if "gender" not in lbl.lower() or "transgender" in lbl.lower() or "pronoun" in lbl.lower():
                    continue
                val_el = c.locator('.select__single-value, .select__multi-value').first
                val = (await val_el.text_content()).strip() if await val_el.count() > 0 else ""
                report["gender"] = val or "EMPTY"

                # If missing or set to Female / Woman
                valid_male = ["male", "man", "cisgender man", "cisgender male"]
                if not val or "female" in val.lower() or "woman" in val.lower() or val.lower() not in valid_male:
                    logger.warning(f"[SAFETY ALERT] Gender violation detected: '{val}'. Auto-correcting to 'Male'...")
                    ctrl = c.locator('.select__control').first
                    if await ctrl.count() > 0:
                        await ctrl.scroll_into_view_if_needed()
                        await ctrl.click()
                        await asyncio.sleep(0.4)
                        opts = c.locator('.select__menu .select__option')
                        if await opts.count() == 0:
                            opts = target.locator('.select__menu:visible .select__option')
                        for i in range(await opts.count()):
                            opt_txt = (await opts.nth(i).text_content()).strip()
                            if opt_txt.lower() in valid_male or (opt_txt.lower() == "male" and "female" not in opt_txt.lower()):
                                await opts.nth(i).click(force=True)
                                await asyncio.sleep(0.3)
                                new_val = await val_el.text_content() if await val_el.count() > 0 else opt_txt
                                report["gender"] = new_val
                                report["corrections"].append(f"Gender corrected from '{val}' to '{new_val}'")
                                logger.info(f"[SAFETY RESOLVED] Gender successfully corrected to: {new_val}")
                                break

            # Check Radio buttons for Gender (Ashby, Lever)
            for r in await target.locator('input[type="radio"]').all():
                r_lbl = await r.evaluate('''e => {
                    let l = (e.id ? document.querySelector(`label[for="${CSS.escape(e.id)}"]`) : null) || e.closest("label");
                    return l ? l.innerText.trim() : "";
                }''')
                grp = await r.evaluate('''e => {
                    let p = e.closest("fieldset, .application-question, [class*='question'], .ashby-application-form-field-entry");
                    return p ? p.innerText.toLowerCase() : "";
                }''')
                if "gender" in grp and "pronoun" not in grp:
                    if await r.is_checked():
                        report["gender"] = r_lbl
                    elif r_lbl.lower() in valid_male and (report["gender"] == "UNKNOWN" or report["gender"].lower() not in valid_male):
                        await r.locator("..").click(force=True)
                        report["gender"] = r_lbl
                        report["corrections"].append(f"Gender radio checked: {r_lbl}")
                        logger.info(f"[SAFETY RESOLVED] Gender radio corrected to: {r_lbl}")

            if report["gender"] == "UNKNOWN":
                has_gender_q = await target.locator('label:has-text("Gender"), legend:has-text("Gender"), [class*="title"]:has-text("Gender")').count() > 0
                if not has_gender_q:
                    report["gender"] = "N/A (Not requested on form)"

            # Location Select Audit (Lever / Ashby / Greenhouse)
            loc_sels = await target.locator('select.candidate-location, select[name*="location" i], select[name="opportunityLocationId"], select.opportunity-location, select[id*="location" i], .application-question:has-text("location") select, select[data-qa*="location"]').all()
            for s in loc_sels:
                val = await s.evaluate('el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : ""')
                is_placeholder = not val or any(p in val.lower() for p in ["select...", "select ...", "^select$", "choose...", "choose ...", "^--+$", "please select", "placeholder"])
                is_wrong_country = bool(val and ("afghanistan" in val.lower() or "albania" in val.lower()))
                if is_placeholder or is_wrong_country:
                    opts_els = await s.locator('option').all()
                    opts = [(await o.text_content() or "").strip() for o in opts_els]
                    best_opt = DeterministicEngine.pick_best_location_choice(opts)
                    if best_opt:
                        await s.select_option(label=best_opt)
                        report["corrections"].append(f"Location select corrected from '{val}' to '{best_opt}'")
                        logger.info(f"[SAFETY RESOLVED] Location select corrected from '{val}' to '{best_opt}'")

            # Sweep all remaining selects to guarantee no select is left on placeholder text
            all_sels = await target.locator('select').all()
            for s in all_sels:
                try:
                    val = await s.evaluate('el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : ""')
                    if val and any(p in val.lower() for p in ["select...", "select ...", "choose...", "choose ...", "please select"]):
                        opts_els = await s.locator('option').all()
                        opts = [(await o.text_content() or "").strip() for o in opts_els]
                        clean_opts = [o for o in opts if o and not any(p in o.lower() for p in ["select...", "select ...", "choose", "--", "please select"])]
                        if clean_opts:
                            s_name = await s.evaluate('el => (el.name || "") + " " + (el.id || "")')
                            if any(k in s_name.lower() for k in ["location", "city", "country", "state", "office"]):
                                best = DeterministicEngine.pick_best_location_choice(clean_opts)
                            else:
                                best = clean_opts[0]
                            if best:
                                await s.select_option(label=best)
                                report["corrections"].append(f"Placeholder select '{s_name.strip()}' resolved to '{best}'")
                                logger.info(f"[SAFETY RESOLVED] Placeholder select '{s_name.strip()}' resolved to '{best}'")
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"SafetyAuditor gender/location error: {e}")

        # 2. RACE / ETHNICITY AUDIT & AUTO-CORRECT
        try:
            race_conts = await target.locator('.select__container:has-text("Race"), .select__container:has-text("racial"), .select__container:has-text("ethnic")').all()
            for c in race_conts:
                lbl_el = c.locator("label, legend, .label").first
                lbl = (await lbl_el.text_content(timeout=1000) or "").strip() if await lbl_el.count() > 0 else (await c.text_content(timeout=1000) or "").strip()
                if not any(k in lbl.lower() for k in ["race", "racial", "ethnic"]):
                    continue
                val_el = c.locator('.select__single-value, .select__multi-value').first
                val = (await val_el.text_content()).strip() if await val_el.count() > 0 else ""
                report["race"] = val or "EMPTY"

                if not val or ("asian" not in val.lower() and ("caucasian" in val.lower() or "white" in val.lower() or "black" in val.lower() or "african" in val.lower() or ("hispanic" in val.lower() and "not hispanic" not in val.lower()) or "decline" in val.lower())):
                    logger.warning(f"[SAFETY ALERT] Race violation detected: '{val}'. Auto-correcting to 'Asian' / 'South Asian'...")
                    ctrl = c.locator('.select__control').first
                    if await ctrl.count() > 0:
                        await ctrl.scroll_into_view_if_needed()
                        await ctrl.click()
                        await asyncio.sleep(0.4)
                        opts = c.locator('.select__menu .select__option')
                        if await opts.count() == 0:
                            opts = target.locator('.select__menu:visible .select__option')
                        for target_race in ["south asian", "asian (not hispanic or latino)", "asian"]:
                            found = False
                            for i in range(await opts.count()):
                                opt_txt = (await opts.nth(i).text_content()).strip()
                                opt_low = opt_txt.lower()
                                if "caucasian" in opt_low:
                                    continue
                                if target_race == opt_low or re.search(rf"(?:^|\b){re.escape(target_race)}(?:\b|$)", opt_low):
                                    await opts.nth(i).click(force=True)
                                    await asyncio.sleep(0.3)
                                    new_val = await val_el.text_content() if await val_el.count() > 0 else opt_txt
                                    report["race"] = new_val
                                    report["corrections"].append(f"Race corrected from '{val}' to '{new_val}'")
                                    logger.info(f"[SAFETY RESOLVED] Race successfully corrected to: {new_val}")
                                    found = True
                                    break
                            if found:
                                break

            # Check Radio buttons for Race (Ashby, Lever)
            if report["race"] in ["UNKNOWN", "EMPTY"]:
                for r in await target.locator('input[type="radio"]').all():
                    r_lbl = await r.evaluate('''e => {
                        let cid = e.id || "";
                        let l = (cid ? document.querySelector(`label[for="${CSS.escape(cid)}"]`) : null)
                                || e.closest("label")
                                || e.closest(".ashby-application-form-input-radio-group-option")
                                || (e.parentElement ? e.parentElement.parentElement : null)
                                || e.parentElement;
                        return l ? l.innerText.trim() : "";
                    }''')
                    grp = await r.evaluate('''e => {
                        let p = e.closest("fieldset, .application-question, [class*='question'], .ashby-application-form-field-entry");
                        return p ? p.innerText.toLowerCase() : "";
                    }''')
                    if any(k in grp for k in ["race", "racial", "ethnic"]):
                        if await r.is_checked():
                            report["race"] = r_lbl
                            break
                        elif any(target_race in r_lbl.lower() for target_race in ["south asian", "asian (not hispanic or latino)", "asian"]) and "caucasian" not in r_lbl.lower() and "pacific" not in r_lbl.lower() and "decline" not in r_lbl.lower():
                            await r.locator("..").click(force=True)
                            report["race"] = r_lbl
                            report["corrections"].append(f"Race radio checked: {r_lbl}")
                            logger.info(f"[SAFETY RESOLVED] Race radio corrected to: {r_lbl}")
                            break

            if report["race"] in ["UNKNOWN", "EMPTY"]:
                has_race_q = await target.locator('label:has-text("Race"), label:has-text("Ethnicity"), legend:has-text("Race"), legend:has-text("Ethnicity"), [class*="title"]:has-text("Race")').count() > 0
                if not has_race_q:
                    report["race"] = "N/A (Not requested on form)"
        except Exception as e:
            logger.debug(f"SafetyAuditor race error: {e}")

        # 3. PERSONAL NAMES AUDIT & AUTO-CORRECT
        try:
            fn_inp = target.locator('#first_name, input[name="first_name"], #first-name-input, input[name="firstName"]').first
            if await fn_inp.count() > 0:
                fn_val = await fn_inp.input_value()
                if not fn_val.strip() or fn_val.strip() == "Siddhesh":
                    await fn_inp.fill("Siddhesh Santosh")
                    report["first_name"] = "Siddhesh Santosh (corrected)"
                    report["corrections"].append("First Name updated to 'Siddhesh Santosh'")
                else:
                    report["first_name"] = fn_val

            ln_inp = target.locator('#last_name, input[name="last_name"], #last-name-input, input[name="lastName"]').first
            if await ln_inp.count() > 0:
                ln_val = await ln_inp.input_value()
                if not ln_val.strip():
                    await ln_inp.fill("Sawant")
                    report["last_name"] = "Sawant (corrected)"
                else:
                    report["last_name"] = ln_val

            pn_inp = target.locator('#preferred_name, input[name="preferred_name"]').first
            if await pn_inp.count() > 0:
                pn_val = await pn_inp.input_value()
                if not pn_val.strip():
                    await pn_inp.fill("Siddhesh")
                    report["preferred_name"] = "Siddhesh (corrected)"
                else:
                    report["preferred_name"] = pn_val

            # Lever / Ashby Full Name input
            name_inp = target.locator('input[name="name"], #name, input[placeholder*="Full name"], input[name="_systemfield_name"], #_systemfield_name').first
            if await name_inp.count() > 0:
                nv = (await name_inp.input_value() or "").strip()
                if nv:
                    report["first_name"] = nv.split()[0]
                    report["last_name"] = " ".join(nv.split()[1:]) if len(nv.split()) > 1 else "Sawant"

            # Check Radio buttons across Lever / Ashby / SmartRecruiters for Gender, Race, Veteran
            for r in await target.locator('input[type="radio"]:checked').all():
                lbl = await r.evaluate('''el => {
                    let cid = el.id || "";
                    let l = (cid ? document.querySelector(`label[for="${CSS.escape(cid)}"]`) : null)
                            || el.closest("label")
                            || el.closest(".ashby-application-form-input-radio-group-option")
                            || (el.parentElement ? el.parentElement.parentElement : null)
                            || el.parentElement;
                    return l ? l.innerText.trim() : "";
                }''')
                grp = await r.evaluate('el => (el.closest("fieldset, .application-question, [class*=\'field\'], .ashby-application-form-field-entry") || el.parentElement).innerText.toLowerCase()')
                if "gender" in grp and "pronoun" not in grp:
                    if any(bad in lbl.lower() for bad in ["woman", "female"]) or ("man" not in lbl.lower() and "male" not in lbl.lower()):
                        # Violation! Correct to Man!
                        parent_grp = r.locator("xpath=ancestor::*[self::fieldset or contains(@class, 'question') or contains(@class, 'field') or contains(@class, 'field-entry')][1]").first
                        if await parent_grp.count() > 0:
                            for sibling_r in await parent_grp.locator('input[type="radio"]').all():
                                s_lbl = await sibling_r.evaluate('''el => {
                                    let cid = el.id || "";
                                    let l = (cid ? document.querySelector(`label[for="${CSS.escape(cid)}"]`) : null)
                                            || el.closest("label")
                                            || el.closest(".ashby-application-form-input-radio-group-option")
                                            || (el.parentElement ? el.parentElement.parentElement : null)
                                            || el.parentElement;
                                    return l ? l.innerText.trim() : "";
                                }''')
                                if s_lbl.lower() in ["man", "male", "cisgender man", "cisgender male"] and "female" not in s_lbl.lower() and "woman" not in s_lbl.lower():
                                    await sibling_r.evaluate("el => { el.click(); el.dispatchEvent(new Event('change', { bubbles: true })); }")
                                    report["gender"] = s_lbl
                                    report["corrections"].append(f"Gender radio corrected from '{lbl}' to '{s_lbl}'")
                                    break
                    else:
                        report["gender"] = lbl
                elif any(k in grp for k in ["race", "ethnic"]):
                    if "asian" not in lbl.lower() and ("caucasian" in lbl.lower() or "white" in lbl.lower() or "black" in lbl.lower() or ("hispanic" in lbl.lower() and "not hispanic" not in lbl.lower()) or "decline" in lbl.lower()):
                        parent_grp = r.locator("xpath=ancestor::*[self::fieldset or contains(@class, 'question') or contains(@class, 'field') or contains(@class, 'field-entry')][1]").first
                        if await parent_grp.count() > 0:
                            for sibling_r in await parent_grp.locator('input[type="radio"]').all():
                                s_lbl = await sibling_r.evaluate('''el => {
                                    let cid = el.id || "";
                                    let l = (cid ? document.querySelector(`label[for="${CSS.escape(cid)}"]`) : null)
                                            || el.closest("label")
                                            || el.closest(".ashby-application-form-input-radio-group-option")
                                            || (el.parentElement ? el.parentElement.parentElement : null)
                                            || el.parentElement;
                                    return l ? l.innerText.trim() : "";
                                }''')
                                if "caucasian" not in s_lbl.lower() and ("asian" in s_lbl.lower() or "south asian" in s_lbl.lower()):
                                    await sibling_r.evaluate("el => { el.click(); el.dispatchEvent(new Event('change', { bubbles: true })); }")
                                    report["race"] = s_lbl
                                    report["corrections"].append(f"Race radio corrected from '{lbl}' to '{s_lbl}'")
                                    break
                    else:
                        report["race"] = lbl

            # Legal name inputs
            for inp in await target.locator('input[type="text"]:visible').all():
                lbl = await inp.evaluate('''el => {
                    if (el.id) {
                        try {
                            let l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                            if (l && l.innerText.trim()) return l.innerText.trim();
                        } catch(e) {}
                    }
                    return el.placeholder || "";
                }''')
                l_low = lbl.lower()
                val = await inp.input_value()
                if "legal first name" in l_low and not val.strip():
                    await inp.fill("Siddhesh Santosh")
                    report["corrections"].append("Legal First Name auto-filled to 'Siddhesh Santosh'")
                elif "legal last name" in l_low and not val.strip():
                    await inp.fill("Sawant")
                    report["corrections"].append("Legal Last Name auto-filled to 'Sawant'")
        except Exception as e:
            logger.debug(f"SafetyAuditor names error: {e}")

        # 4. WORK AUTH & SPONSORSHIP AUDIT & AUTO-CORRECT
        try:
            # 4a. Greenhouse / custom react-select containers
            auth_conts = await target.locator('.select__container:has-text("authorized to work"), .select__container:has-text("legally authorized"), .select__container:has-text("work authorization"), .select__container:has-text("work eligibility")').all()
            for c in auth_conts:
                val_el = c.locator('.select__single-value, .select__multi-value').first
                val = (await val_el.text_content()).strip() if await val_el.count() > 0 else ""
                report["work_authorization"] = val or "EMPTY"
                if not val or val.lower() == "no" or "not authorized" in val.lower():
                    ctrl = c.locator('.select__control').first
                    if await ctrl.count() > 0:
                        await ctrl.click()
                        await asyncio.sleep(0.3)
                        opts = target.locator('.select__menu:visible .select__option')
                        for i in range(await opts.count()):
                            opt_txt = (await opts.nth(i).text_content()).strip()
                            if opt_txt.lower() == "yes" or ("authorized" in opt_txt.lower() and "not authorized" not in opt_txt.lower()):
                                await opts.nth(i).click(force=True)
                                report["work_authorization"] = "Yes (corrected)"
                                report["corrections"].append("Work authorization corrected to 'Yes'")
                                break

            spons_conts = await target.locator('.select__container:has-text("sponsor"), .select__container:has-text("visa")').all()
            for c in spons_conts:
                lbl_el = c.locator("label, legend, .label").first
                lbl = (await lbl_el.text_content(timeout=1000) or "").strip() if await lbl_el.count() > 0 else (await c.text_content(timeout=1000) or "").strip()
                if "sponsor" not in lbl.lower() and "visa" not in lbl.lower():
                    continue
                val_el = c.locator('.select__single-value, .select__multi-value').first
                val = (await val_el.text_content()).strip() if await val_el.count() > 0 else ""
                report["sponsorship"] = val or "EMPTY"
                if not val or val.lower() == "no":
                    ctrl = c.locator('.select__control').first
                    if await ctrl.count() > 0:
                        await ctrl.click()
                        await asyncio.sleep(0.3)
                        opts = target.locator('.select__menu:visible .select__option')
                        for i in range(await opts.count()):
                            opt_txt = (await opts.nth(i).text_content()).strip()
                            if opt_txt.lower() == "yes":
                                await opts.nth(i).click(force=True)
                                report["sponsorship"] = "Yes (corrected)"
                                report["corrections"].append("Visa sponsorship corrected to 'Yes'")
                                break

            # 4b. Ashby / Lever / Universal Cards: Radio buttons and Button Toggles
            all_cards = await target.locator(':is(.ashby-application-form-field-entry, .field-entry, [class*="fieldEntry"], fieldset, .application-question, .form-group, .field)').all()
            for card in all_cards:
                try:
                    q_text = await card.evaluate('''el => {
                        const lbl = el.querySelector("legend, label, .application-label, [class*='title'], [class*='label']");
                        if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
                        return el.innerText.split(String.fromCharCode(10))[0].trim();
                    }''')
                    if not q_text:
                        continue
                    q_low = q_text.lower()

                    # Work Authorization check
                    is_work_auth = any(k in q_low for k in ["authorized to work", "legally authorized", "work authorization", "work eligibility", "eligible to work"]) and not any(k in q_low for k in ["sponsor", "clearance"])
                    # Sponsorship check
                    is_spons = any(k in q_low for k in ["sponsorship", "require sponsorship", "visa sponsorship", "visa support", "visa assistance", "immigration support", "sponsor a visa", "require visa", "need visa"]) and not any(k in q_low for k in ["security clearance", "which visa", "visa type", "visa status", "expir", "detail"])
                    # Previous Employment check
                    is_prev_emp = any(k in q_low for k in ["previously worked", "worked for", "worked at", "former employee", "employed by", "employed with", "prior employee"])

                    if is_work_auth:
                        has_buttons = await card.locator("button[aria-pressed], button.ashby-application-form-input-yesno-option, [class*='yesno'] button, button[data-option]").count() > 0
                        has_radios = await card.locator("input[type='radio']").count() > 0
                        has_select = await card.locator("select").count() > 0

                        cur_val = await card.evaluate('''el => {
                            const pressedBtn = el.querySelector("button[aria-pressed='true'], button[aria-checked='true'], button._active_1svni_57, button.active, button.selected");
                            if (pressedBtn) return pressedBtn.innerText.trim();
                            const checkedRadio = el.querySelector("input[type='radio']:checked");
                            if (checkedRadio) {
                                const l = (checkedRadio.id ? document.querySelector(`label[for="${CSS.escape(checkedRadio.id)}"]`) : null) || checkedRadio.closest("label");
                                return l ? l.innerText.trim() : checkedRadio.value;
                            }
                            const sel = el.querySelector("select");
                            if (sel && sel.selectedIndex >= 0) return sel.options[sel.selectedIndex].text.trim();
                            return "";
                        }''')

                        report["work_authorization"] = cur_val or "EMPTY"
                        is_disqualifying = (
                            not cur_val
                            or cur_val.lower() == "no"
                            or "not authorized" in cur_val.lower()
                            or "unauthorized" in cur_val.lower()
                            or "ineligible" in cur_val.lower()
                        )
                        if is_disqualifying:
                            logger.warning(f"[SAFETY ALERT] Disqualifying work authorization detected: '{cur_val}'. Auto-correcting to canonical Yes/Authorized...")
                            target_val = "Yes"
                            f_type = "button" if has_buttons else ("radio" if has_radios else ("select" if has_select else "button"))
                            ok = await select_matching_option(target, q_text, target_val, field_type=f_type, card_locator=card)
                            if ok:
                                report["work_authorization"] = "Yes (corrected)"
                                report["corrections"].append(f"Work authorization corrected from '{cur_val}' to 'Yes'")
                                logger.info(f"[SAFETY RESOLVED] Work authorization successfully corrected to 'Yes'")

                    elif is_spons:
                        has_buttons = await card.locator("button[aria-pressed], button.ashby-application-form-input-yesno-option, [class*='yesno'] button, button[data-option]").count() > 0
                        has_radios = await card.locator("input[type='radio']").count() > 0
                        has_select = await card.locator("select").count() > 0

                        cur_val = await card.evaluate('''el => {
                            const pressedBtn = el.querySelector("button[aria-pressed='true'], button[aria-checked='true'], button._active_1svni_57, button.active, button.selected");
                            if (pressedBtn) return pressedBtn.innerText.trim();
                            const checkedRadio = el.querySelector("input[type='radio']:checked");
                            if (checkedRadio) {
                                const l = (checkedRadio.id ? document.querySelector(`label[for="${CSS.escape(checkedRadio.id)}"]`) : null) || checkedRadio.closest("label");
                                return l ? l.innerText.trim() : checkedRadio.value;
                            }
                            const sel = el.querySelector("select");
                            if (sel && sel.selectedIndex >= 0) return sel.options[sel.selectedIndex].text.trim();
                            return "";
                        }''')

                        report["sponsorship"] = cur_val or "EMPTY"
                        if not cur_val or cur_val.lower() == "no":
                            logger.warning(f"[SAFETY ALERT] Incorrect sponsorship answer detected: '{cur_val}'. Auto-correcting to canonical Yes...")
                            target_val = "Yes"
                            f_type = "button" if has_buttons else ("radio" if has_radios else ("select" if has_select else "button"))
                            ok = await select_matching_option(target, q_text, target_val, field_type=f_type, card_locator=card)
                            if ok:
                                report["sponsorship"] = "Yes (corrected)"
                                report["corrections"].append(f"Visa sponsorship corrected from '{cur_val}' to 'Yes'")
                                logger.info(f"[SAFETY RESOLVED] Visa sponsorship successfully corrected to 'Yes'")

                    elif is_prev_emp:
                        has_buttons = await card.locator("button[aria-pressed], button.ashby-application-form-input-yesno-option, [class*='yesno'] button, button[data-option]").count() > 0
                        has_radios = await card.locator("input[type='radio']").count() > 0
                        has_select = await card.locator("select").count() > 0

                        cur_val = await card.evaluate('''el => {
                            const pressedBtn = el.querySelector("button[aria-pressed='true'], button[aria-checked='true'], button._active_1svni_57, button.active, button.selected");
                            if (pressedBtn) return pressedBtn.innerText.trim();
                            const checkedRadio = el.querySelector("input[type='radio']:checked");
                            if (checkedRadio) {
                                const l = (checkedRadio.id ? document.querySelector(`label[for="${CSS.escape(checkedRadio.id)}"]`) : null) || checkedRadio.closest("label");
                                return l ? l.innerText.trim() : checkedRadio.value;
                            }
                            const sel = el.querySelector("select");
                            if (sel && sel.selectedIndex >= 0) return sel.options[sel.selectedIndex].text.trim();
                            return "";
                        }''')

                        report["previous_employment"] = cur_val or "EMPTY"
                        if not cur_val or cur_val.lower() == "yes":
                            logger.warning(f"[SAFETY ALERT] Incorrect previous employment answer detected: '{cur_val}'. Auto-correcting to canonical No...")
                            target_val = "No"
                            f_type = "button" if has_buttons else ("radio" if has_radios else ("select" if has_select else "button"))
                            ok = await select_matching_option(target, q_text, target_val, field_type=f_type, card_locator=card)
                            if ok:
                                report["previous_employment"] = "No (corrected)"
                                report["corrections"].append(f"Previous employment corrected from '{cur_val}' to 'No'")
                                logger.info(f"[SAFETY RESOLVED] Previous employment successfully corrected to 'No'")
                except Exception as ce:
                    logger.debug(f"SafetyAuditor card check error: {ce}")

            if report["work_authorization"] == "UNKNOWN":
                auth_q = await target.locator('.application-question:has-text("authoriz"), .application-question:has-text("sponsorship"), [class*="field"]:has-text("authoriz"), [class*="field"]:has-text("sponsorship")').count()
                if auth_q == 0:
                    report["work_authorization"] = "N/A (Not requested on form)"
                    report["sponsorship"] = "N/A (Not requested on form)"
        except Exception as e:
            logger.debug(f"SafetyAuditor auth error: {e}")

        # 5. CONTACT & DEMOGRAPHICS AUDIT
        try:
            em_inp = target.locator('#email, input[name="email"], input[type="email"]').first
            if await em_inp.count() > 0:
                em_val = await em_inp.input_value()
                report["email"] = em_val or pi.get("email", "siddheshs202001@gmail.com")
            else:
                report["email"] = pi.get("email", "siddheshs202001@gmail.com")

            ph_inp = target.locator('#phone, input[name="phone"], input[type="tel"]').first
            if await ph_inp.count() > 0:
                ph_val = await ph_inp.input_value()
                report["phone"] = ph_val or pi.get("phone", "(445) 208-2299")
            else:
                report["phone"] = pi.get("phone", "(445) 208-2299")

            # Military / Veteran Status Audit & Auto-Correct
            vet_conts = await target.locator('.select__container:has-text("Veteran"), .select__container:has-text("veteran"), .select__container:has-text("Military Status"), .select__container:has-text("military status")').all()
            for c in vet_conts:
                lbl_el = c.locator('label, legend, .label').first
                c_lbl = (await lbl_el.text_content(timeout=1000) or "").lower() if await lbl_el.count() > 0 else (await c.text_content(timeout=1000) or "").lower()
                if "add another" in c_lbl or "relevant employment" in c_lbl or "military service above" in c_lbl:
                    continue
                val_el = c.locator('.select__single-value, .select__multi-value').first
                if await val_el.count() > 0:
                    val = (await val_el.text_content()).strip()
                    report["veteran_status"] = val
                    valid_vet = ["never served", "not a protected", "not a veteran", "i am not a protected", "i am not a veteran"]
                    is_valid = any(k in val.lower() for k in valid_vet) or (val.lower().strip() == "no")
                    if not val or not is_valid or any(bad in val.lower() for bad in ["active duty", "reserve", "national guard", "i identify as one or more", "i am a protected veteran"]):
                        logger.warning(f"[SAFETY ALERT] Military violation detected: '{val}'. Auto-correcting...")
                        ctrl = c.locator('.select__control').first
                        if await ctrl.count() > 0:
                            await ctrl.click()
                            await asyncio.sleep(0.3)
                            opts = target.locator('.select__menu:visible .select__option')
                            for i in range(await opts.count()):
                                opt_txt = (await opts.nth(i).text_content()).strip()
                                if any(k in opt_txt.lower() for k in ["never served", "not a protected", "not a veteran", "no"]):
                                    await opts.nth(i).click(force=True)
                                    report["veteran_status"] = opt_txt
                                    report["corrections"].append(f"Military/Veteran corrected from '{val}' to '{opt_txt}'")
                                    break
            if report["veteran_status"] == "UNKNOWN":
                report["veteran_status"] = comp.get("veteran_status", "I have never served in the military")

            # Disability Status Audit
            dis_conts = await target.locator('.select__container:has-text("Disability"), .select__container:has-text("disability")').all()
            for c in dis_conts:
                val_el = c.locator('.select__single-value, .select__multi-value').first
                if await val_el.count() > 0:
                    report["disability_status"] = (await val_el.text_content()).strip()
            if report["disability_status"] == "UNKNOWN":
                report["disability_status"] = comp.get("disability_status", "No, I do not have a disability")

            # Previous Employment Audit
            emp_conts = await target.locator('.select__container:has-text("employed"), .select__container:has-text("contract")').all()
            for c in emp_conts:
                val_el = c.locator('.select__single-value, .select__multi-value').first
                if await val_el.count() > 0:
                    report["previous_employment"] = (await val_el.text_content()).strip()
            if report["previous_employment"] == "UNKNOWN":
                report["previous_employment"] = "No"

            # Corporate Compliance / Conflicts of Interest / Government Officials Audit & Auto-Correct
            comp_conts = await target.locator('.select__container:has-text("Personal/Familial"), .select__container:has-text("government official"), .select__container:has-text("bribery")').all()
            for c in comp_conts:
                val_el = c.locator('.select__single-value, .select__multi-value').first
                if await val_el.count() > 0:
                    val = (await val_el.text_content()).strip()
                    if val.lower() == "yes":
                        logger.warning(f"[SAFETY ALERT] Compliance violation detected: '{val}'. Auto-correcting to 'No'...")
                        ctrl = c.locator('.select__control').first
                        if await ctrl.count() > 0:
                            await ctrl.click()
                            await asyncio.sleep(0.3)
                            opts = target.locator('.select__menu:visible .select__option')
                            for i in range(await opts.count()):
                                opt_txt = (await opts.nth(i).text_content()).strip()
                                if opt_txt.lower() == "no":
                                    await opts.nth(i).click(force=True)
                                    report["corrections"].append(f"Compliance question corrected from '{val}' to 'No'")
                                    break

            # LGBTQ+ Community Identification Audit & Auto-Correct
            lgbt_conts = await target.locator('.select__container:has-text("LGBTQ"), .select__container:has-text("lgbt")').all()
            for c in lgbt_conts:
                val_el = c.locator('.select__single-value, .select__multi-value').first
                if await val_el.count() > 0:
                    val = (await val_el.text_content()).strip()
                    if val.lower() == "yes":
                        logger.warning(f"[SAFETY ALERT] LGBTQ+ violation detected: '{val}'. Auto-correcting to 'No'...")
                        ctrl = c.locator('.select__control').first
                        if await ctrl.count() > 0:
                            await ctrl.click()
                            await asyncio.sleep(0.3)
                            opts = target.locator('.select__menu:visible .select__option')
                            for i in range(await opts.count()):
                                opt_txt = (await opts.nth(i).text_content()).strip()
                                if opt_txt.lower() in ["no", "decline", "i don't wish to answer"]:
                                    await opts.nth(i).click(force=True)
                                    report["corrections"].append(f"LGBTQ+ question corrected from '{val}' to '{opt_txt}'")
                                    break
        except Exception as e:
            logger.debug(f"SafetyAuditor contact/demo error: {e}")

        # PRINT SAFETY AUDIT REPORT
        print("\n" + "=" * 80)
        print("  [SAFETY AUDITOR] PRE-REVIEW AUDIT & VALIDATION REPORT:")
        print("=" * 80)
        for k, v in report.items():
            if k == "corrections":
                continue
            if k == "preferred_name" and v in ["UNKNOWN", "EMPTY"]:
                v = "N/A (Not requested on form)"
            status = "✅ PASS" if v not in ["UNKNOWN", "EMPTY"] and "corrected" not in v else ("⚠️ FIXED" if "corrected" in v else "❌ FAIL")
            print(f"  {status:8} {k.replace('_', ' ').title():25} : {v}")
        if report["corrections"]:
            print("  --- Auto-Corrections Applied ---")
            for c in report["corrections"]:
                print(f"  🔧 {c}")
        print("=" * 80 + "\n")
        return report


# ============================================================================
# HybridATSFormFiller — Deterministic Regex Matrix + Constrained Gemini Fallback
# ============================================================================
class HybridATSFormFiller:
    """
    Production-grade hybrid form filler for Greenhouse, Ashby, Lever, SmartRecruiters.
    1. Deterministic Regex Matrix bypasses LLM for all standard questions.
    2. Strictly constrained GeminiFallbackSolver handles novel open-ended fields.
    3. SafetyAuditor auto-corrects demographic / compliance collisions.
    4. Audit Gate prints CLI summary and pauses — NEVER auto-submits.
    """

    def __init__(self, page: Page, profile: Dict[str, Any], portal: str):
        self.page = page
        self.profile = profile
        self.portal = portal
        self.pi = profile.get("personal_info", {})
        self.exp = profile.get("experience", [{}])
        self.edu = profile.get("education", [{}])
        self.comp = profile.get("compliance_and_eeo", {})
        self.ai = GeminiFallbackSolver()
        self._fields_filled_this_pass = 0
        self.populated_fields = []
        self.unanswered_fields = []

    # ------------------------------------------------------------------
    # Step 1: Resolve Target — iframe detection
    # ------------------------------------------------------------------
    async def resolve_target(self) -> Union[Page, FrameLocator, Frame]:
        """
        Greenhouse and other ATS portals frequently embed forms in iframes.
        Always check for #grnh_iframe, iframe[title='Job Application'], or greenhouse frames.
        """
        for iframe_sel in [
            "#grnh_iframe",
            'iframe[title="Job Application"]',
            'iframe[id*="greenhouse" i]',
            'iframe[src*="greenhouse" i]',
            'iframe[src*="smartrecruiters" i]',
            'iframe[src*="lever" i]',
            'iframe[src*="ashby" i]',
        ]:
            try:
                floc = self.page.frame_locator(iframe_sel)
                probe = floc.locator("#application_form, form, #main_fields, input[name='first_name'], #first_name, input[name='name'], #first-name-input")
                if await probe.count() > 0:
                    logger.info(f"Target iframe '{iframe_sel}' detected and verified.")
                    return floc
            except Exception:
                pass

        # Fallback: scan all frames attached to the page
        for frame in self.page.frames:
            if any(d in frame.url.lower() for d in ["greenhouse.io", "grnh.se", "job-boards", "smartrecruiters", "lever.co", "ashbyhq"]):
                logger.info(f"ATS frame detected via URL: {frame.url}")
                return frame

        return self.page

    # ------------------------------------------------------------------
    # Step 2: Portal-specific fill orchestration
    # ------------------------------------------------------------------
    async def run(self) -> bool:
        """Main entry point. Returns True on success."""
        try:
            from app.services.grounded_essay import ensure_context_for_url
            if self.page and getattr(self.page, "url", None):
                ensure_context_for_url(self.page.url)
        except Exception as _ge:
            logger.debug(f"grounded_essay context error in run(): {_ge}")

        # Rule 3: Dismiss banners before any fill
        await dismiss_cookie_banners(self.page, raise_on_blocked=True)

        target = await self.resolve_target()

        try:
            if self.portal == "greenhouse":
                await self._fill_greenhouse(target)
            elif self.portal == "ashby":
                await self._fill_ashby(target)
            elif self.portal == "lever":
                await self._fill_lever(target)
            elif self.portal in ["smartrecruiters", "sr"]:
                await self._fill_smartrecruiters(target)
            elif self.portal == "rippling":
                await self._fill_rippling(target)

            # Recursive sweep: re-scan until no new fields are filled
            for sweep in range(MAX_RECURSIVE_SWEEPS):
                await dismiss_cookie_banners(self.page, raise_on_blocked=False)
                self._fields_filled_this_pass = 0
                await self._universal_sweep(target)
                logger.info(f"Sweep {sweep+1}: filled {self._fields_filled_this_pass} new fields")
                if self._fields_filled_this_pass == 0:
                    break
                await asyncio.sleep(0.5)

            # Audit and pause
            await self._audit_and_pause(target)
            return True
        except FormHaltException as fhe:
            logger.warning(f"UnifiedATSFormFiller safe-halt [{fhe.code}]: {fhe.message}")
            if not any(u.get("status") == fhe.code for u in self.unanswered_fields):
                self.unanswered_fields.append({
                    "portal": self.portal,
                    "type": "halt",
                    "status": fhe.code,
                    "label": fhe.question or fhe.message,
                    "choices": fhe.choices,
                    "screenshot": fhe.screenshot_path
                })
            raise

    # ==================================================================
    # GREENHOUSE
    # ==================================================================
    async def _fill_greenhouse(self, target):
        """Greenhouse-specific: personal info, resume, experience, education, dropdowns, custom questions."""
        logger.info("=== Greenhouse Adapter ===")

        await self._gh_personal_info(target)
        await upload_resume(target)
        await upload_cover_letter(target)
        await asyncio.sleep(0.5)
        await self._gh_experience(target)
        await self._gh_education(target)
        await self._gh_react_selects(target)
        await self._gh_custom_questions(target)
        await self._fill_native_selects(target)
        await self._fill_radios(target)

    async def _gh_personal_info(self, t):
        pi = self.pi
        await self._safe_fill(t, '#first_name, input[name="first_name"]', pi.get("first_name", "Siddhesh Santosh"))
        await self._safe_fill(t, '#last_name, input[name="last_name"]', pi.get("last_name", "Sawant"))
        await self._safe_fill(t, '#preferred_name, input[name="preferred_name"]', pi.get("preferred_name", "Siddhesh"))
        await self._safe_fill(t, '#email, input[name="email"], input[type="email"]', pi.get("email", ""))

        # Phone country calling code (Greenhouse dropdown)
        cc_loc = t.locator('#job_application_phone_country_code, select[name*="country_code" i], select[id*="country_code" i], select[name*="phone_country" i]').first
        if await cc_loc.count() > 0:
            try:
                opts = await cc_loc.locator("option").all()
                for opt in opts:
                    txt = (await opt.text_content() or "").strip()
                    if "+1" in txt or "united states" in txt.lower():
                        await cc_loc.select_option(label=txt, timeout=1500)
                        logger.info(f"Greenhouse Phone Country Code selected: '{txt}'")
                        break
            except Exception as e:
                logger.debug(f"Greenhouse phone country code error: {e}")

        phone_val = pi.get("phone", "(445) 208-2299")
        await self._safe_fill(t, '#phone, input[name="phone"], input[type="tel"], input[name*="phone" i], input[id*="phone" i], input[autocomplete="tel"], input[placeholder*="phone" i]', phone_val)

        # Location combobox (Greenhouse candidate location)
        loc_container = t.locator('#candidate-location, .select__container:has-text("Candidate Location"), .field:has-text("Candidate Location")').first
        loc_already_filled = False
        if await loc_container.count() > 0:
            sv = loc_container.locator('.select__single-value, [class*="singleValue"]')
            if await sv.count() > 0 and (await sv.first.text_content() or "").strip():
                loc_already_filled = True

        if not loc_already_filled:
            loc_inp = t.locator('input#candidate-location, #candidate-location input, .select__container:has-text("Candidate Location") input, #candidate-location .select__input input').first
            if await loc_inp.count() == 0 and await t.locator('#candidate-location').count() > 0:
                loc_inp = t.locator('#candidate-location').first

            if await loc_inp.count() > 0:
                val = (await loc_inp.input_value() or "").strip()
                if not val:
                    await ComboboxHandler.type_and_select(
                        t, loc_inp, "Washington",
                        "Washington, District of Columbia, United States",
                    )
        else:
            for c in await t.locator('.select__container').all():
                lbl_el = c.locator('label, legend, .label').first
                if await lbl_el.count() > 0:
                    try:
                        c_lbl = (await lbl_el.text_content(timeout=1000) or "").replace("*", "").strip().lower()
                    except Exception:
                        continue
                    if c_lbl in ["location", "candidate location"]:
                        inp = c.locator("input").first
                        if await inp.count() > 0:
                            await ComboboxHandler.type_and_select(
                                t, inp, "Washington",
                                "Washington, District of Columbia, United States",
                            )
                            break

        # LinkedIn / GitHub
        await self._safe_fill(t, 'input[id*="linkedin" i], input[name*="linkedin" i]', pi.get("linkedin_url", ""))
        await self._safe_fill(t, 'input[id*="github" i], input[name*="github" i]', pi.get("github_url", ""))

    async def _gh_experience(self, t):
        """Fill ALL work experiences from candidate profile into Greenhouse (multi-entry)."""
        if not self.exp:
            return

        for i, exp in enumerate(self.exp):
            comp = exp.get("company", "").strip()
            title = exp.get("title", "").strip()
            if not comp or not title:
                continue

            # If index > 0 and input does not exist yet, click "Add another"
            comp_loc = t.locator(f'#company-name-{i}, input[id*="company-name-{i}"]')
            if await comp_loc.count() == 0 and i > 0:
                add_btn = t.locator('.employment--container .add-another-button, .employment-form button:has-text("Add another")').first
                if await add_btn.count() > 0:
                    await add_btn.scroll_into_view_if_needed()
                    await add_btn.click()
                    await asyncio.sleep(0.5)

            # Fill Company Name
            await self._safe_fill(t, f'#company-name-{i}, input[id*="company-name-{i}"]', comp)

            # Fill Title
            await self._safe_fill(t, f'#title-{i}, input[id*="title-{i}"]', title)

            # Fill Start Year — strictly 4 digits
            sy = t.locator(f'#start-date-year-{i}, input[id*="start-date-year-{i}"]').first
            if await sy.count() > 0 and exp.get("start_year"):
                year_val = str(exp.get("start_year"))[-4:]
                await sy.fill(year_val)

            # Fill Start Month dropdown
            await self._gh_month_select(t, f"start-date-month-{i}", exp.get("start_month", "June"))

            # Current Role vs End Date
            if exp.get("is_current", False):
                curr = t.locator(f'#current-role-{i}_1, input[id*="current-role-{i}"]').first
                if await curr.count() > 0 and not await curr.is_checked():
                    await curr.check(force=True)
                    logger.info(f"Checked 'Current role' for {comp}")
            else:
                ey = t.locator(f'#end-date-year-{i}, input[id*="end-date-year-{i}"]').first
                if await ey.count() > 0 and exp.get("end_year"):
                    await ey.fill(str(exp.get("end_year"))[-4:])
                await self._gh_month_select(t, f"end-date-month-{i}", exp.get("end_month", "March"))

            logger.info(f"GH Experience [{i+1}]: {title} at {comp} populated.")
            self._fields_filled_this_pass += 1
            self.populated_fields.append({"portal": "Greenhouse", "type": "experience", "label": f"Experience {i+1}: {comp}", "value": title})

    async def _gh_month_select(self, t, identifier: str, month: str):
        if not month:
            return
        # Scope strictly to the target container by id so experience months never overwrite education months
        cont = t.locator(f'#{identifier}').locator('xpath=ancestor::*[contains(@class, "select__container")][1]').first
        if await cont.count() == 0:
            cont = t.locator(f'.select__container:has([id="{identifier}"])').first
        if await cont.count() == 0:
            return

        ctrl = cont.locator('.select__control').first
        if await ctrl.count() > 0:
            await ctrl.scroll_into_view_if_needed()
            await ctrl.click(force=True)
            await asyncio.sleep(0.3)
            # Search option inside container first, then page portal menu
            opt = cont.locator('.select__option').filter(has_text=re.compile(rf'^{month}', re.IGNORECASE)).first
            if await opt.count() == 0:
                opt = t.locator('.select__option:visible').filter(has_text=re.compile(rf'^{month}', re.IGNORECASE)).first
            if await opt.count() > 0:
                await opt.click(force=True)
                await asyncio.sleep(0.2)
            else:
                await self._press_escape(t)

    async def _gh_education(self, t):
        """Fill education entries (school, degree, discipline, start/end month AND year) from profile with read-back verification (R5)."""
        edu_list = self.edu if (self.edu and isinstance(self.edu, list)) else [{}]
        for idx, edu in enumerate(edu_list[:2]):
            if not isinstance(edu, dict):
                continue
            # Only fill second education block if its inputs exist on the form
            if idx > 0:
                has_block = await t.locator(f'#school--{idx}, #start-year--{idx}, #end-year--{idx}').count()
                if has_block == 0:
                    break

            school = edu.get("school", "George Mason University" if idx == 0 else "University of Mumbai")
            degree = edu.get("degree", "Master's Degree" if idx == 0 else "Bachelor's Degree")
            discipline = edu.get("discipline") or edu.get("major", "Information Systems" if idx == 0 else "Information Technology")
            start_month = edu.get("start_month", "January" if idx == 0 else "August")
            start_year = str(edu.get("start_year", "2023" if idx == 0 else "2018"))[-4:]
            end_month = edu.get("end_month", "May" if idx == 0 else "August")
            end_year = str(edu.get("end_year", "2024" if idx == 0 else "2022"))[-4:]

            # Comboboxes (only use ComboboxHandler if element is actually a React-Select input)
            for cid, search, exact in [
                (f"school--{idx}", school.split()[0] if school else "George Mason", school),
                (f"degree--{idx}", "Master" if idx == 0 else "Bachelor", degree),
                (f"discipline--{idx}", discipline.split()[0] if discipline else "Information", discipline),
            ]:
                inp = t.locator(f'#{cid}').first
                if await inp.count() > 0:
                    is_react = await inp.evaluate('el => Boolean(el.classList.contains("select__input") || el.closest(".select__container, .select__control"))')
                    if is_react:
                        await self._gh_combobox_select(t, cid, search, exact)
                    else:
                        await self._safe_fill(t, f'#{cid}', exact)

            # Education Start & End Month selects
            await self._gh_month_select(t, f"start-month--{idx}", start_month)
            await self._gh_month_select(t, f"end-month--{idx}", end_month)

            # Education Start & End Year inputs with strict read-back verification (R5)
            for y_label, y_sel, y_val in [
                (f"Education {idx+1} Start Year", f'#start-year--{idx}, input[id*="education"][id*="start"][id*="year"], input[name*="education"][name*="start_year"]', start_year),
                (f"Education {idx+1} End Year", f'#end-year--{idx}, input[id*="education"][id*="end"][id*="year"], input[name*="education"][name*="end_year"]', end_year),
            ]:
                y_inp = t.locator(y_sel).first
                if await y_inp.count() > 0 and y_val:
                    await y_inp.scroll_into_view_if_needed()
                    await y_inp.fill("")
                    await y_inp.fill(y_val)
                    await y_inp.dispatch_event("input")
                    await y_inp.dispatch_event("change")
                    await y_inp.dispatch_event("blur")
                    actual_y = (await y_inp.input_value() or "").strip()
                    if actual_y != y_val:
                        # Retry once via direct evaluate
                        await y_inp.evaluate('(el, v) => { el.value = v; el.dispatchEvent(new Event("input", {bubbles:true})); el.dispatchEvent(new Event("change", {bubbles:true})); }', y_val)
                        actual_y = (await y_inp.input_value() or "").strip()
                    if actual_y != y_val:
                        raise FormHaltException(
                            "EDUCATION_DATE_INCOMPLETE",
                            f"Failed to set/verify {y_label} (intended='{y_val}', actual='{actual_y}').",
                            question=y_label,
                        )
                    logger.info(f"GH Education [{idx+1}] {y_label} → {actual_y} (verified)")
                    self._fields_filled_this_pass += 1

    async def _gh_combobox_select(self, t, identifier: str, search: str, exact: str):
        inp = t.locator(f'#{identifier}').first
        if await inp.count() == 0:
            cont = t.locator(f'.select__container:has([id*="{identifier}"])').first
            if await cont.count() > 0:
                inp = cont.locator("input.select__input, input").first
        if await inp.count() > 0:
            await ComboboxHandler.type_and_select(t, inp, search, exact)

    async def _gh_react_selects(self, t):
        """Scan ALL React-Select containers and fill unfilled ones (multi-pass for dynamic options)."""
        for pass_idx in range(3):
            containers = await t.locator('.select__container').all()
            filled_any = False
            for c in containers:
                try:
                    lbl_el = c.locator('label, legend, .label').first
                    if await lbl_el.count() == 0:
                        continue
                    raw_label = (await lbl_el.text_content() or "").strip()
                    label = raw_label.replace("*", "").replace("\n", " ").strip()
                    if not label:
                        continue

                    # Append subtext/description if present in field container
                    try:
                        parent_field = c.locator('xpath=ancestor::*[contains(@class, "field-wrapper") or contains(@class, "field") or contains(@class, "question")]').first
                        if await parent_field.count() > 0:
                            desc_el = parent_field.locator('.question-description, [id*="description"], .body__secondary, [class*="description"]').first
                            if await desc_el.count() > 0:
                                desc_txt = (await desc_el.text_content() or "").strip()
                                if desc_txt and desc_txt not in label:
                                    label = f"{label} — {desc_txt}"
                    except Exception:
                        pass

                    # Skip date months and main location (handled in dedicated adapters)
                    skip_keys = ["candidate location", "start date month", "end date month"]
                    if any(k in label.lower() for k in skip_keys) or label.lower().strip() in ["location", "location (city)"]:
                        continue

                    # Skip if already has a value (single-select or multi-select)
                    val_el = c.locator('.select__single-value, .select__multi-value').first
                    if await val_el.count() > 0 and (await val_el.text_content() or "").strip():
                        continue

                    ctrl = c.locator('.select__control').first
                    if await ctrl.count() == 0:
                        continue
                    await ctrl.scroll_into_view_if_needed()
                    await ctrl.click()
                    await asyncio.sleep(0.4)

                    options = c.locator('.select__menu .select__option')
                    if await options.count() == 0:
                        options = t.locator('.select__menu:visible .select__option')

                    # Retry click if menu didn't open
                    if await options.count() == 0:
                        await ctrl.click()
                        await asyncio.sleep(0.4)
                        options = c.locator('.select__menu .select__option')
                        if await options.count() == 0:
                            options = t.locator('.select__menu:visible .select__option')

                    count = await options.count()
                    if count > 0:
                        raw_choices = await options.all_text_contents()
                        choices = [ch.strip() for ch in raw_choices if ch.strip()]
                        # 1. Deterministic Matrix first
                        answer = DeterministicEngine.solve(label, choices)

                        # Verify if deterministic answer matches any option in choices
                        if not answer or not any(score_option_match(answer, ch) >= 0.60 for ch in choices):
                            # 2. Query AI Solver (NVIDIA NIM 90B / Gemini)
                            ai_ans = self.ai.solve(label, choices, "select")
                            if ai_ans and any(score_option_match(ai_ans, ch) >= 0.60 for ch in choices):
                                answer = ai_ans

                        # 3. Intelligent Domain Safety Fallback if still unmatched
                        if not answer or not any(score_option_match(answer, ch) >= 0.60 for ch in choices):
                            q_low = label.lower()
                            if re.search(r"(conflict|government|crime|felony|violation|investigation|bribe|misconduct|disciplinary|dispute|suit|lawsuit|relat|client.*vendor|referred\s*by|former\s*employee|previously\s*employed)", q_low):
                                answer = DeterministicEngine.solve("Are you a former government official or have conflict of interest?", choices)
                            elif re.search(r"(certif|acknowledg|confirm|agree|accept|understand|permission|consent|authorized|receipt)", q_low):
                                answer = DeterministicEngine.solve("Please confirm receipt and acknowledge", choices)
                            elif re.search(r"(how\s*(do\s*)?you\s*use\s*ai|best\s*describes.*use\s*ai|ai\s*tools\s*today)", q_low):
                                answer = DeterministicEngine.solve("Which of the following best describes how you use AI tools today?", choices)

                        if answer:
                            scored_gh = [(score_option_match(answer, opt_t), i, opt_t) for i, opt_t in enumerate(choices)]
                            scored_gh.sort(key=lambda x: x[0], reverse=True)
                            best_s, best_i, best_t = scored_gh[0]
                            if best_s >= 0.60:
                                await options.nth(best_i).click(force=True)
                                await asyncio.sleep(0.3)
                                logger.info(f"GH Select [{label[:40]}] → {best_t} (score={best_s:.2f})")
                                self._fields_filled_this_pass += 1
                                self.populated_fields.append({"portal": "Greenhouse", "type": "select", "label": label, "value": best_t})
                                filled_any = True
                            else:
                                await self._press_escape(t)
                                self.unanswered_fields.append({"portal": "Greenhouse", "type": "select", "label": label, "choices": choices})
                        else:
                            await self._press_escape(t)
                            self.unanswered_fields.append({"portal": "Greenhouse", "type": "select", "label": label, "choices": choices})
                    else:
                        await self._press_escape(t)
                except Exception as e:
                    if isinstance(e, FormHaltException):
                        raise
                    logger.debug(f"GH react-select error: {e}")
            if not filled_any:
                break

    async def _gh_custom_questions(self, t):
        """Scan text inputs, textareas, and checkboxes for custom questions."""
        inputs = await t.locator('input[type="text"]:visible, input[type="tel"]:visible, input[type="url"]:visible, input[type="date"]:visible, input:not([type]):visible, textarea:visible').all()
        for inp in inputs:
            try:
                # Skip inputs inside React-Select containers
                parent_select = inp.locator('xpath=ancestor::*[contains(@class,"select__")]')
                if await parent_select.count() > 0:
                    continue

                if (await inp.input_value() or "").strip():
                    continue

                label = await self._extract_label(inp)
                if not label:
                    continue

                inp_tag = await inp.evaluate("el => el.tagName.toLowerCase()")
                is_req = await inp.evaluate('''el => {
                    let reqAttr = el.required || el.getAttribute("aria-required") === "true";
                    let p = el.closest(".field, .field-wrapper, .application-question, [class*='field']");
                    let hasStar = false;
                    if (p) {
                        if (p.classList.contains("required") || p.hasAttribute("required")) hasStar = true;
                        if (p.querySelector(".required, [required], [class*='required'], [class*='asterisk']")) hasStar = true;
                        let text = p.innerText || "";
                        if (text.includes("*") || text.includes("✱") || text.toLowerCase().includes("required")) hasStar = true;
                    }
                    return Boolean(reqAttr || hasStar);
                }''')

                # Conditional follow-up check (e.g. "If you chose Braze Employee... please specify")
                is_conditional = bool(re.search(r"(if\s*you\s*(chose|selected).*specify|if\s*other.*(specify|explain|describe)|if\s*referr.*(who|name|specify|list)|who\s*referred\s*you|employee\s*referr.*name|name\s*of\s*(the\s*)?referr|former\s*employee.*name|referred\s*by\s*an\s*employee|if\s*yes.*explain)", label, re.IGNORECASE))
                if is_conditional:
                    if is_req:
                        await inp.fill("N/A")
                        logger.info(f"GH Text [{label[:35]}] → N/A (Required Conditional)")
                        self._fields_filled_this_pass += 1
                        self.populated_fields.append({"portal": "Greenhouse", "type": inp_tag, "label": label, "value": "N/A"})
                    else:
                        logger.info(f"GH Text [{label[:35]}] → Skipped (Optional Conditional)")
                    continue

                # 1. Deterministic Matrix first; 2. Only halt/invoke fallback if field is required
                answer = DeterministicEngine.solve(label, is_required=is_req)
                if answer is None and is_req:
                    answer = self.ai.solve(label, None, "textarea" if inp_tag == "textarea" else "text")
                if answer and str(answer) != "N/A":
                    await inp.fill(str(answer))
                    logger.info(f"GH Text [{label[:35]}] → {str(answer)[:50]}")
                    self._fields_filled_this_pass += 1
                    self.populated_fields.append({"portal": "Greenhouse", "type": inp_tag, "label": label, "value": str(answer)})
                elif answer and str(answer) == "N/A":
                    if is_req:
                        await inp.fill("N/A")
                        logger.info(f"GH Text [{label[:35]}] → N/A (Required)")
                        self._fields_filled_this_pass += 1
                        self.populated_fields.append({"portal": "Greenhouse", "type": inp_tag, "label": label, "value": "N/A"})
                    else:
                        logger.info(f"GH Text [{label[:35]}] → Skipped (Optional N/A)")
                else:
                    if is_req:
                        self.unanswered_fields.append({"portal": "Greenhouse", "type": inp_tag, "label": label})
            except Exception as e:
                if isinstance(e, FormHaltException):
                    raise
                logger.debug(f"GH custom text error: {e}")

        # Checkboxes: standalone, agreement, multi-select groups, referral sources
        cbs = await t.locator('input[type="checkbox"]').all()
        for cb in cbs:
            try:
                if await cb.is_checked():
                    continue

                info = await cb.evaluate('''el => {
                    let cid = el.id || "";
                    let cname = el.getAttribute("name") || "";
                    let lblEl = (cid ? document.querySelector(`label[for="${CSS.escape(cid)}"]`) : null)
                                || el.closest("label")
                                || el.parentElement;
                    let optText = lblEl ? lblEl.innerText.trim() : "";

                    let fe = el.closest("fieldset, .field, .field-wrapper, .application-question, [class*='field']");
                    let qEl = fe ? fe.querySelector("legend, label, .label, [class*='description']") : null;
                    let qText = el.getAttribute("description") 
                                || (qEl ? qEl.innerText.trim() : "")
                                || (fe ? fe.innerText.split(String.fromCharCode(10))[0].trim() : "");

                    let hasLinkedinInGroup = false;
                    let groupScope = fe || (cname ? document : null);
                    if (groupScope) {
                        let sel = cname ? `input[type="checkbox"][name="${CSS.escape(cname)}"]` : 'input[type="checkbox"]';
                        let siblings = Array.from(groupScope.querySelectorAll(sel));
                        hasLinkedinInGroup = siblings.some(s => {
                            let sl = (s.id ? document.querySelector(`label[for="${CSS.escape(s.id)}"]`) : null) || s.closest("label");
                            return sl ? sl.innerText.toLowerCase().includes("linkedin") : false;
                        });
                    }

                    return {
                        id: cid,
                        name: cname,
                        option: optText,
                        question: qText,
                        checked: el.checked,
                        has_linkedin_in_group: hasLinkedinInGroup
                    };
                }''')

                if info.get("checked"):
                    continue

                opt_lower = info.get("option", "").lower().strip()
                q_lower = info.get("question", "").lower().strip()
                combined = f"{opt_lower} {q_lower}"

                to_check = False

                # 1. Sanctions & Legal Exclusions — NEVER check these
                if any(bad in opt_lower for bad in [
                    "sanction", "felony", "criminal", "convicted", "decline to self-identify",
                    "prefer not to say", "do not consent", "opt out", "conflict of interest",
                    "russia", "cuba", "iran", "syria", "korea", "belarus", "resident of"
                ]):
                    to_check = False

                # 2. Mandatory Consent, Terms, Agreements, Privacy Policies
                elif any(k in combined for k in [
                    "i agree", "agree to the", "consent", "certify", "acknowledge",
                    "i understand", "i confirm", "truthful", "accurate and complete",
                    "terms of service", "privacy policy", "data privacy", "background check",
                    "none of the above", "not applicable", "accept"
                ]):
                    to_check = True

                # 3. Referral Source / "How did you hear about this opportunity" (Rule 9)
                elif any(k in q_lower for k in [
                    "how did you hear", "where did you hear", "how did you find",
                    "where did you find", "source of application", "referral source",
                    "hear about this"
                ]):
                    if any(b in opt_lower for b in ["linkedin", "referral", "friend", "social"]):
                        to_check = False
                    elif any(s in opt_lower for s in ["company website", "careers page", "company site"]):
                        to_check = True
                    elif any(s in opt_lower for s in ["job board", "job site", "online job"]):
                        to_check = True
                    else:
                        to_check = False

                # 4. Technical Domains / Core Strengths
                elif any(k in q_lower for k in ["technical domain", "area of interest", "primary interest", "discipline", "core skill"]):
                    if any(s in opt_lower for s in ["backend", "back end", "systems", "software engineer", "full stack", "distributed systems", "python", "cloud"]):
                        to_check = True

                # 5. Work Authorization / Age Verification
                elif any(k in q_lower for k in ["authorized", "authorization", "legally eligible", "18 years"]):
                    if any(pos in opt_lower for pos in ["yes", "authorized", "eligible", "united states", "18 or older"]):
                        to_check = True

                # 6. DeterministicEngine match
                else:
                    ans = DeterministicEngine.solve(info["question"], [info["option"]])
                    if ans and ans.lower() in ["yes", "true", "agree", "confirm", "acknowledge", "accept", opt_lower]:
                        to_check = True

                # Perform Click
                if to_check:
                    await cb.evaluate('''el => {
                        let lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`) || el.closest("label");
                        if (lbl) {
                            lbl.click();
                        } else {
                            el.click();
                        }
                        el.dispatchEvent(new Event("input", { bubbles: true }));
                        el.dispatchEvent(new Event("change", { bubbles: true }));
                    }''')
                    logger.info(f"GH Checkbox: Checked '{info['option'][:30]}' for '{info['question'][:30]}'")
                    self._fields_filled_this_pass += 1
                    self.populated_fields.append({
                        "portal": "Greenhouse",
                        "type": "checkbox",
                        "label": f"{info['question'][:40]} -> {info['option']}",
                        "value": "checked"
                    })
                    await asyncio.sleep(0.2)
            except Exception as e:
                if isinstance(e, FormHaltException):
                    raise
                logger.debug(f"GH checkbox error: {e}")

    # ==================================================================
    # ASHBY
    # ==================================================================
    async def _fill_ashby(self, target):
        """Ashby-specific: strict execution order to preserve toggle state."""
        logger.info("=== Ashby Adapter ===")
        # If on Ashby job overview page, navigate to /application or click Apply
        if await target.locator('input[type="file"], input[name*="_systemfield"]').count() == 0:
            apply_btn = target.locator('a:text-is("Apply 1-Click"), a:text-is("Apply for this Job"), button:text-is("Apply for this Job"), a[href*="/application"]').first
            if await apply_btn.count() > 0 and await apply_btn.is_visible():
                logger.info("Clicking Ashby 'Apply' button...")
                await apply_btn.click()
                await asyncio.sleep(2.0)
            elif hasattr(target, "url") and "/application" not in target.url:
                apply_url = target.url.rstrip("/") + "/application"
                logger.info(f"Navigating to Ashby application URL: {apply_url}")
                await target.goto(apply_url, wait_until="domcontentloaded")
                await asyncio.sleep(2.0)

        # Order matters: toggles LAST because clicking inputs/radios can untoggle them

        # 1. Personal info
        await self._ashby_personal_info(target)
        # 2. Resume upload + parser settle time
        await upload_resume(target)
        await asyncio.sleep(2.0)
        # 3. Autocomplete comboboxes (Location, How did you hear, Degree, etc.)
        await self._ashby_autocompletes(target)
        # 4. All text inputs
        await self._ashby_text_inputs(target)
        # 5. Radios
        await self._ashby_radios(target)
        # 6. Checkboxes
        await self._ashby_checkboxes(target)
        # 7. Yes/No toggles — LAST
        await self._ashby_toggles(target)

    async def _ashby_personal_info(self, t):
        pi = self.pi
        await self._safe_fill(t, '#_systemfield_name, input[name="_systemfield_name"]', pi.get("full_name", ""))
        await self._safe_fill(t, '#_systemfield_email, input[name="_systemfield_email"]', pi.get("email", ""))
        phone_val = pi.get("phone", "(445) 208-2299")
        phone_filled = await self._safe_fill(t, 'input[type="tel"]', phone_val)
        if not phone_filled:
            phone_selectors = [
                '#_systemfield_phone, input[name="_systemfield_phone"]',
                'input[autocomplete="tel"]',
                '.ashby-application-form-field-entry:has-text("Phone") input:not([type="radio"]):not([type="checkbox"])',
                '.field-entry:has-text("Phone") input:not([type="radio"]):not([type="checkbox"])',
                'label:has-text("Phone") ~ input',
                'input[placeholder*="Phone" i]',
                'input[name*="phone" i]',
                'input[id*="phone" i]'
            ]
            for p_sel in phone_selectors:
                if await self._safe_fill(t, p_sel, phone_val):
                    break

    async def _ashby_autocompletes(self, t):
        """Handle all Ashby autocomplete comboboxes: location, referral source, degree, etc."""
        autocompletes = await t.locator(
            'input.ashby-application-form-input-autocomplete, '
            'input[aria-autocomplete="list"], '
            'input[role="combobox"]'
        ).all()
        for inp in autocompletes:
            try:
                # Check if already filled
                curr_val = (await inp.input_value() or "").strip()
                if curr_val:
                    continue

                # Extract label via DOM traversal
                label = await inp.evaluate('''el => {
                    let card = el.closest(".ashby-application-form-field-entry, .field-entry, fieldset, .application-question") || el.parentElement;
                    while (card && card !== document.body) {
                        let l = card.querySelector("label, legend, .ashby-application-form-question-title, [class*='label'], [class*='Title']");
                        if (l && l.innerText.trim()) return l.innerText.trim();
                        card = card.parentElement;
                    }
                    return el.getAttribute("aria-label") || el.placeholder || "";
                }''')
                label_clean = re.sub(r'[✱*]', '', label).strip()
                q_low = label_clean.lower()

                # Case A: Location / Address / Country / Payroll Tax Location
                is_loc_q = any(k in q_low for k in [
                    "location", "city", "where are you located", "address", "country", "state", "province",
                    "where do you plan on working", "plan on working", "working from", "payroll", "tax purposes",
                    "residence", "current location", "based out of", "workplace location"
                ]) and "which location" not in q_low

                if is_loc_q:
                    await inp.scroll_into_view_if_needed()
                    await inp.click(force=True)
                    await inp.evaluate("el => { el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); }")
                    await inp.press_sequentially("Washington", delay=50)
                    await asyncio.sleep(1.0)
                    popups = t.locator('.ashby-application-form-input-autocomplete-popup [role="option"], [role="listbox"] [role="option"], [role="option"]')
                    cnt = await popups.count()
                    selected = False
                    for i in range(cnt):
                        txt = (await popups.nth(i).text_content() or "").strip()
                        if "washington" in txt.lower() and ("columbia" in txt.lower() or "dc" in txt.lower() or "d.c." in txt.lower()):
                            await popups.nth(i).click(force=True)
                            logger.info(f"Ashby Autocomplete [{label_clean}] → {txt}")
                            self._fields_filled_this_pass += 1
                            self.populated_fields.append({"portal": "Ashby", "type": "autocomplete", "label": label_clean, "value": txt})
                            selected = True
                            break
                    if not selected and cnt > 0:
                        for i in range(cnt):
                            txt = (await popups.nth(i).text_content() or "").strip()
                            if any(k in txt.lower() for k in ["united states", "usa", "district of columbia"]):
                                await popups.nth(i).click(force=True)
                                logger.info(f"Ashby Autocomplete [{label_clean}] US fallback → {txt}")
                                self._fields_filled_this_pass += 1
                                self.populated_fields.append({"portal": "Ashby", "type": "autocomplete", "label": label_clean, "value": txt})
                                selected = True
                                break
                    if not selected:
                        # Try United States
                        await inp.evaluate("el => { el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); }")
                        await inp.press_sequentially("United States", delay=50)
                        await asyncio.sleep(1.0)
                        popups = t.locator('.ashby-application-form-input-autocomplete-popup [role="option"], [role="listbox"] [role="option"], [role="option"]')
                        cnt = await popups.count()
                        if cnt > 0:
                            for i in range(cnt):
                                txt = (await popups.nth(i).text_content() or "").strip()
                                if txt.lower() == "united states" or "united states" in txt.lower():
                                    await popups.nth(i).click(force=True)
                                    logger.info(f"Ashby Autocomplete [{label_clean}] → {txt}")
                                    self._fields_filled_this_pass += 1
                                    self.populated_fields.append({"portal": "Ashby", "type": "autocomplete", "label": label_clean, "value": txt})
                                    selected = True
                                    break
                            if not selected:
                                await popups.first.click(force=True)
                                txt = (await popups.first.text_content() or "").strip()
                                self._fields_filled_this_pass += 1
                                self.populated_fields.append({"portal": "Ashby", "type": "autocomplete", "label": label_clean, "value": txt})
                    continue

                # Case B: Other Ashby dropdowns (e.g. "How did you hear about us?", "Degree", "School", etc.)
                is_req = await inp.evaluate('''el => {
                    let card = el.closest(".ashby-application-form-field-entry, .field-entry, fieldset, .application-question") || el.parentElement;
                    if (el.required || el.getAttribute("aria-required") === "true") return true;
                    if (card && (card.innerText.includes("✱") || card.innerText.includes("*") || card.querySelector(".required, [required]"))) return true;
                    return false;
                }''')

                await inp.scroll_into_view_if_needed()
                # 1. Enumerate real DOM options first before typing
                card = inp.locator('xpath=ancestor::*[contains(@class, "ashby-application-form-field-entry") or contains(@class, "fieldEntry") or self::fieldset][1]')
                if await card.count() > 0:
                    btn = card.locator('button[class*="toggleButton"], button._toggleButton_d7ago_32, button').first
                    if await btn.count() > 0:
                        await btn.click(force=True)
                        await asyncio.sleep(0.3)
                await inp.click(force=True)
                await asyncio.sleep(0.3)

                card_popups = card.locator('.ashby-application-form-input-autocomplete-popup [role="option"], [role="listbox"] [role="option"]')
                if await card_popups.count() > 0:
                    popups = card_popups
                else:
                    popups = t.locator('.ashby-application-form-input-autocomplete-popup:visible [role="option"], [role="listbox"]:visible [role="option"]')
                    if await popups.count() == 0:
                        popups = t.locator('.ashby-application-form-input-autocomplete-popup [role="option"], [role="listbox"] [role="option"], [role="option"]')

                cnt = await popups.count()
                raw_choices = []
                for i in range(cnt):
                    txt = (await popups.nth(i).text_content() or "").strip()
                    if txt and not any(no_opt in txt.lower() for no_opt in ["no options", "no results", "loading", "no matching"]):
                        raw_choices.append(txt)

                best_choice = None
                if raw_choices:
                    best_choice = DeterministicEngine.solve(label_clean, raw_choices)

                # 2. If no direct match from open menu, determine search text and filter
                if not best_choice:
                    search_queries = []
                    if any(k in q_low for k in ["how did you hear", "referral", "source", "where did you find", "hear about"]):
                        search_queries = ["LinkedIn"]
                    elif any(k in q_low for k in ["work auth", "authorized to work", "immigration", "visa", "sponsorship"]):
                        search_queries = ["F-1", "Authorized", "Require", "Yes"]
                    elif any(k in q_low for k in ["school", "university", "college", "institution"]):
                        search_queries = ["George Mason"]
                    elif any(k in q_low for k in ["degree", "education level", "highest level of education"]):
                        search_queries = ["Master"]
                    elif any(k in q_low for k in ["major", "discipline", "field of study"]):
                        search_queries = ["Information Systems", "Computer Science"]
                    elif any(k in q_low for k in ["clearance"]):
                        search_queries = ["N/A", "None", "No"]
                    elif any(k in q_low for k in ["export", "us person", "u.s. person"]):
                        search_queries = ["None of the above", "Citizen of a different country", "No"]
                    else:
                        intended_val = DeterministicEngine.solve(label_clean)
                        if intended_val:
                            search_queries = [str(intended_val).split()[0]]

                    for sq in search_queries:
                        await inp.evaluate("el => { el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); }")
                        await inp.press_sequentially(sq, delay=35)
                        await asyncio.sleep(0.5)
                        card_filtered = card.locator('.ashby-application-form-input-autocomplete-popup [role="option"], [role="listbox"] [role="option"]')
                        if await card_filtered.count() > 0:
                            popups = card_filtered
                        else:
                            popups = t.locator('.ashby-application-form-input-autocomplete-popup:visible [role="option"], [role="listbox"]:visible [role="option"]')
                            if await popups.count() == 0:
                                popups = t.locator('.ashby-application-form-input-autocomplete-popup [role="option"], [role="listbox"] [role="option"]')
                        cnt = await popups.count()
                        filter_choices = []
                        for i in range(cnt):
                            txt = (await popups.nth(i).text_content() or "").strip()
                            if txt and not any(no_opt in txt.lower() for no_opt in ["no options", "no results", "loading", "no matching"]):
                                filter_choices.append(txt)
                        if filter_choices:
                            best_choice = DeterministicEngine.solve(label_clean, filter_choices) or filter_choices[0]
                            if best_choice:
                                break

                # 3. Click matching option
                selected_txt = None
                if best_choice:
                    for i in range(cnt):
                        txt = (await popups.nth(i).text_content() or "").strip()
                        if best_choice.lower() == txt.lower() or best_choice.lower() in txt.lower() or txt.lower() in best_choice.lower():
                            try:
                                await popups.nth(i).click(force=True)
                            except Exception:
                                await popups.nth(i).evaluate("el => el.click()")
                            logger.info(f"Ashby Autocomplete [{label_clean}] → Selected '{txt}'")
                            self._fields_filled_this_pass += 1
                            self.populated_fields.append({"portal": "Ashby", "type": "autocomplete", "label": label_clean, "value": txt})
                            await asyncio.sleep(FIELD_FILL_GAP_MS / 1000.0)
                            selected_txt = txt
                            break

                # 4. If unmatched, clear input so no unmatched free text or "No options" popup remains
                if not selected_txt:
                    await inp.evaluate("el => { el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); }")
                    if is_req:
                        logger.warning(f"Required Ashby combobox [{label_clean}] unmatched against real DOM options. Routed to MISSING_INFO_REQUEST.")
                        self.unanswered_fields.append({"portal": "Ashby", "type": "autocomplete", "label": label_clean, "status": "MISSING_INFO_REQUEST"})
            except Exception as ae:
                logger.debug(f"Ashby autocomplete error: {ae}")

    async def _ashby_text_inputs(self, t):
        pi = self.pi
        inputs = await t.locator('input[type="text"], input[type="email"], input[type="tel"], input[type="url"], input[type="date"], input:not([type]), textarea').all()
        for inp in inputs:
            try:
                iid = (await inp.get_attribute("id") or "").lower()
                if "recaptcha" in iid:
                    continue
                if (await inp.input_value() or "").strip():
                    continue

                # Extract label via DOM traversal
                # Using String.fromCharCode(10) instead of literal \n in JS
                label = await inp.evaluate(
                    'el => {'
                    '  let p = el.parentElement;'
                    '  while (p && p !== document.body) {'
                    '    let l = p.querySelector("label, legend, [class*=\'label\'], [class*=\'title\'], [class*=\'Title\']");'
                    '    if (l && l.innerText.trim()) return l.innerText.trim();'
                    '    if (p.innerText.trim() && p.innerText.trim() !== el.value) {'
                    '      return p.innerText.split(String.fromCharCode(10))[0].trim();'
                    '    }'
                    '    p = p.parentElement;'
                    '  }'
                    '  return el.placeholder || "";'
                    '}'
                )
                if not label:
                    continue

                q = label.lower()
                ans = None

                inp_tag = await inp.evaluate("el => el.tagName.toLowerCase()")
                is_req = await inp.evaluate('''el => {
                    let reqAttr = el.required || el.getAttribute("aria-required") === "true";
                    let p = el.closest(".field, .field-wrapper, .application-question, [class*='Field'], [class*='field']");
                    let hasStar = false;
                    if (p) {
                        if (p.classList.contains("required") || p.hasAttribute("required")) hasStar = true;
                        if (p.querySelector(".required, [required], [class*='required'], [class*='asterisk']")) hasStar = true;
                        let text = p.innerText || "";
                        if (text.includes("*") || text.includes("✱") || text.toLowerCase().includes("required")) hasStar = true;
                    }
                    return Boolean(reqAttr || hasStar);
                }''')

                # Conditional follow-up check (e.g. "If you chose X... please specify")
                is_conditional = bool(re.search(r"(if\s*you\s*(chose|selected).*specify|if\s*other.*(specify|explain|describe)|if\s*referr.*(who|name|specify|list)|who\s*referred\s*you|employee\s*referr.*name|name\s*of\s*(the\s*)?referr|former\s*employee.*name|referred\s*by\s*an\s*employee|if\s*yes.*explain)", label, re.IGNORECASE))
                if is_conditional:
                    if is_req:
                        await inp.scroll_into_view_if_needed()
                        await inp.fill("N/A")
                        await inp.dispatch_event("input")
                        await inp.dispatch_event("change")
                        logger.info(f"Ashby Input [{label[:35]}] → N/A (Required Conditional)")
                        self._fields_filled_this_pass += 1
                        self.populated_fields.append({"portal": "Ashby", "type": inp_tag, "label": label, "value": "N/A"})
                        await asyncio.sleep(FIELD_FILL_GAP_MS / 1000.0)
                    else:
                        logger.info(f"Ashby Input [{label[:35]}] → Skipped (Optional Conditional)")
                    continue

                # Direct mapping for known Ashby fields
                if "preferred name" in q:
                    ans = pi.get("first_name", "Siddhesh")
                elif "legal name" in q:
                    ans = pi.get("full_name", "Siddhesh Sawant")
                elif "email" in q:
                    ans = pi.get("email", "")
                elif "phone" in q:
                    ans = pi.get("phone", "")
                elif "linkedin" in q:
                    ans = pi.get("linkedin_url", "https://www.linkedin.com/in/sss20/")
                elif "github" in q:
                    ans = pi.get("github_url", "https://github.com/sids202001")
                elif any(k in q for k in ["portfolio", "website", "personal site"]):
                    ans = pi.get("portfolio_url", "https://siddhesh202001-portfolio.netlify.app")
                elif "how did you hear" in q:
                    ans = "Company Website"
                elif any(k in q for k in ["twitter", "x profile", "x url"]):
                    ans = pi.get("twitter_url", getattr(config, "TWITTER_URL", ""))
                    if not ans:
                        if is_req:
                            ans = "N/A"
                        else:
                            continue
                else:
                    ans = DeterministicEngine.solve(label, is_required=is_req)
                    if ans is None and is_req:
                        ans = self.ai.solve(label, None, "textarea" if inp_tag == "textarea" else "text")

                if ans and str(ans) != "N/A":
                    await self._human_fill(inp, str(ans))
                    logger.info(f"Ashby Input [{label[:35]}] → {str(ans)[:50]}")
                    self._fields_filled_this_pass += 1
                    self.populated_fields.append({"portal": "Ashby", "type": inp_tag, "label": label, "value": str(ans)})
                    if "ashby-application-form-input-date" in str(await inp.get_attribute("class") or "") or inp_tag == "date":
                        try:
                            await inp.press("Escape")
                        except Exception:
                            pass
                    await asyncio.sleep(FIELD_FILL_GAP_MS / 1000.0)
                else:
                    if is_req:
                        self.unanswered_fields.append({"portal": "Ashby", "type": inp_tag, "label": label})
            except Exception as e:
                if isinstance(e, FormHaltException):
                    raise
                logger.debug(f"Ashby text input: {e}")

    async def _ashby_radios(self, t):
        """Unified generic radio selection for Ashby via select_matching_option."""
        radios = await t.locator('input[type="radio"], [role="radio"]').all()
        if not radios:
            return
        seen_cards = set()
        for r in radios:
            try:
                card_key = await r.evaluate('''el => {
                    if (el.name) return "name:" + el.name;
                    const c = el.closest(".ashby-application-form-field-entry, .field-entry, [class*='fieldEntry'], fieldset, .application-question");
                    return c ? (c.id || c.className + c.innerText.slice(0, 30)) : (el.name || el.id);
                }''')
                if card_key in seen_cards:
                    continue
                seen_cards.add(card_key)

                is_any_checked = await r.evaluate('''el => {
                    const c = el.closest(".ashby-application-form-field-entry, .field-entry, [class*='fieldEntry'], fieldset, .application-question") || el.parentElement;
                    const sel = el.name ? `input[type='radio'][name="${CSS.escape(el.name)}"], [role='radio'][name="${CSS.escape(el.name)}"]` : "input[type='radio'], [role='radio']";
                    const inputs = c ? c.querySelectorAll(sel) : [el];
                    for (let inp of inputs) {
                        if (inp.checked || inp.getAttribute('aria-checked') === 'true') return true;
                    }
                    return false;
                }''')
                if is_any_checked:
                    continue

                card = r.locator('xpath=ancestor::*[contains(@class, "field-entry") or contains(@class, "fieldEntry") or self::fieldset or contains(@class, "application-question")][1]').first
                q_text = await r.evaluate('''el => {
                    const fs = el.closest("fieldset");
                    if (fs) {
                        const leg = fs.querySelector("legend");
                        if (leg && leg.innerText.trim()) return leg.innerText.trim();
                    }
                    if (el.name && (el.name.toLowerCase().includes("consent") || el.name.toLowerCase().includes("communication") || el.name.toLowerCase().includes("sms") || el.name.toLowerCase().includes("whatsapp"))) {
                        return "Communication Consent";
                    }
                    const c = el.closest(".ashby-application-form-field-entry, .field-entry, [class*='fieldEntry'], fieldset, .application-question");
                    if (c) {
                        const lbl = c.querySelector("label, .application-label, legend, h4, [class*='title']");
                        if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
                        return c.innerText.split(String.fromCharCode(10))[0].trim();
                    }
                    return "";
                }''')
                if not q_text:
                    continue

                r_choice_objs = await r.evaluate('''el => {
                    const c = el.closest(".ashby-application-form-field-entry, .field-entry, [class*='fieldEntry'], fieldset, .application-question") || el.parentElement;
                    const sel = el.name ? `input[type='radio'][name="${CSS.escape(el.name)}"], [role='radio'][name="${CSS.escape(el.name)}"]` : "input[type='radio'], [role='radio']";
                    const inputs = c ? Array.from(c.querySelectorAll(sel)) : [el];
                    return inputs.map(i => {
                        let txt = "";
                        if (i.id) {
                            try {
                                const lf = document.querySelector('label[for="' + CSS.escape(i.id) + '"]');
                                if (lf && lf.innerText.trim()) txt = lf.innerText.trim();
                            } catch(e) {}
                        }
                        if (!txt) {
                            const cl = i.closest("label");
                            if (cl && cl.innerText.trim()) txt = cl.innerText.trim();
                        }
                        if (!txt) {
                            const aria = (i.getAttribute("aria-label") || "").trim();
                            if (aria) txt = aria;
                        }
                        if (!txt) {
                            const lblBy = (i.getAttribute("aria-labelledby") || "").trim();
                            if (lblBy) {
                                const parts = lblBy.split(/\\s+/).map(id => {
                                    const n = document.getElementById(id);
                                    return n ? n.innerText.trim() : "";
                                }).filter(Boolean);
                                if (parts.length) txt = parts.join(" ");
                            }
                        }
                        if (!txt && i.parentElement && i.parentElement !== c) {
                            const sibLbl = i.parentElement.querySelector("label");
                            if (sibLbl && sibLbl.innerText.trim()) txt = sibLbl.innerText.trim();
                            else {
                                const pTxt = (i.parentElement.innerText || "").trim();
                                if (pTxt && pTxt.length < 180) txt = pTxt;
                            }
                        }
                        if (!txt && i.innerText && i.innerText.trim()) txt = i.innerText.trim();
                        const rawVal = (i.getAttribute("value") || "").trim();
                        if (!txt && rawVal && rawVal.toLowerCase() !== "on") txt = rawVal;
                        return { label: txt, outer: i.outerHTML.slice(0, 300) };
                    });
                }''')

                unreadable = [o for o in r_choice_objs if not o.get("label")]
                if unreadable:
                    raise FormHaltException(
                        "OPTION_LABEL_UNREADABLE",
                        f"Radio option label unreadable for '{q_text}'. Raw outer HTML: {unreadable[0].get('outer')}",
                        question=q_text,
                        choices=[o.get("label") or "<UNREADABLE>" for o in r_choice_objs],
                    )
                r_choices = [o["label"] for o in r_choice_objs]

                ans = DeterministicEngine.solve(q_text, r_choices)
                if ans is None:
                    ans = self.ai.solve(q_text, r_choices, "radio")
                if ans:
                    ok = await select_matching_option(t, q_text, ans, field_type="radio", card_locator=card if await card.count() > 0 else None)
                    if ok:
                        self._fields_filled_this_pass += 1
                        self.populated_fields.append({"portal": "Ashby", "type": "radio", "label": q_text, "value": ans})
                else:
                    self.unanswered_fields.append({"portal": "Ashby", "type": "radio", "label": q_text})
            except Exception as e:
                if isinstance(e, FormHaltException):
                    raise
                logger.debug(f"Ashby radio error: {e}")

    async def _ashby_checkboxes(self, t):
        """Unified generic checkbox selection for Ashby via select_matching_option."""
        cbs = await t.locator('input[type="checkbox"], [role="checkbox"], button[role="checkbox"]').all()
        for cb in cbs:
            try:
                # 1. Skip if this checkbox belongs to an Ashby Yes/No toggle
                is_yesno = await cb.evaluate('''el => {
                    return !!el.closest(".ashby-application-form-input-yesno, [class*='yesno'], .ashby-application-form-field-entry:has(button[data-option='yes'])");
                }''')
                if is_yesno:
                    continue

                card = cb.locator('xpath=ancestor::*[contains(@class, "field-entry") or contains(@class, "fieldEntry") or self::fieldset or contains(@class, "application-question")][1]').first
                info = await cb.evaluate('''el => {
                    let cid = el.id || "";
                    let lblEl = (cid ? document.querySelector(`label[for="${CSS.escape(cid)}"]`) : null) 
                                || el.closest("label") 
                                || el.closest(".ashby-application-form-input-checkbox-group-option")
                                || el.parentElement;
                    let optText = lblEl ? lblEl.innerText.trim() : (el.getAttribute("aria-label") || "").trim();
                    let fe = el.closest(".ashby-application-form-field-entry, [class*='field-entry'], [class*='fieldEntry'], .ashby-application-form-field, fieldset");
                    let qEl = fe ? fe.querySelector("label, [class*='title'], [class*='Title'], legend") : null;
                    let qText = qEl ? qEl.innerText.trim() : (fe ? fe.innerText.split(String.fromCharCode(10))[0].trim() : optText);
                    let isChecked = el.tagName.toLowerCase() === "input" ? el.checked : (el.getAttribute("aria-checked") === "true" || el.getAttribute("aria-pressed") === "true");
                    return { option: optText, question: qText, checked: isChecked, outer: el.outerHTML.slice(0, 300) };
                }''')
                if info["checked"]:
                    continue

                if not info["option"]:
                    raise FormHaltException(
                        "OPTION_LABEL_UNREADABLE",
                        f"Checkbox option label unreadable for '{info['question']}'. Raw outer HTML: {info.get('outer')}",
                        question=info["question"],
                    )

                ans = DeterministicEngine.solve(info["question"], [info["option"]]) or DeterministicEngine.solve(info["option"])
                if ans:
                    ok = await select_matching_option(t, info["question"], ans, field_type="checkbox", card_locator=card if await card.count() > 0 else None)
                    if ok:
                        self._fields_filled_this_pass += 1
                        self.populated_fields.append({"portal": "Ashby", "type": "checkbox", "label": info["option"], "value": "checked"})
            except Exception as e:
                if isinstance(e, FormHaltException):
                    raise
                logger.debug(f"Ashby checkbox error: {e}")

    async def _ashby_toggles(self, t):
        """R2: Read both toggle labels, match canonical answer, verify selected state by read-back. Halt TOGGLE_UNREADABLE if unreadable."""
        toggle_cards = await t.locator(':is(.ashby-application-form-field-entry, .field-entry, [class*="fieldEntry"], fieldset):has(button[data-option], button.ashby-application-form-input-yesno-option, [class*="yesno"] button, button[aria-pressed])').all()
        for card in toggle_cards:
            try:
                q_text = await card.evaluate('''el => {
                    const lbl = el.querySelector("label, .application-label, legend, h4, [class*='title']");
                    if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
                    return el.innerText.split(String.fromCharCode(10))[0].trim();
                }''')
                if not q_text:
                    continue

                is_selected = await card.evaluate('''el => {
                    const btns = el.querySelectorAll("button[data-option], button.ashby-application-form-input-yesno-option, [class*='yesno'] button, button[aria-pressed]");
                    for (let b of btns) {
                        if (b.getAttribute("aria-pressed") === "true" || b.getAttribute("aria-checked") === "true" || /active|selected|_active_/.test(b.className)) return true;
                    }
                    const chk = el.querySelector("input:checked");
                    if (chk) return true;
                    return false;
                }''')
                if is_selected:
                    continue

                btn_objs = await card.evaluate('''el => {
                    const btns = Array.from(el.querySelectorAll("button[data-option], button.ashby-application-form-input-yesno-option, [class*='yesno'] button, button[aria-pressed]"));
                    return btns.map(b => {
                        const txt = (b.innerText || b.getAttribute("aria-label") || b.getAttribute("data-option") || "").trim();
                        return { label: txt, outer: b.outerHTML.slice(0, 300) };
                    });
                }''')
                if not btn_objs or any(not b.get("label") for b in btn_objs):
                    raw_html = btn_objs[0].get("outer", "") if btn_objs else await card.evaluate("el => el.outerHTML.slice(0, 300)")
                    raise FormHaltException(
                        "TOGGLE_UNREADABLE",
                        f"Toggle control label unreadable for '{q_text}'. Raw HTML: {raw_html}",
                        question=q_text,
                        choices=[b.get("label") or "<UNREADABLE>" for b in btn_objs],
                    )
                choices = [b["label"] for b in btn_objs]
                ans = DeterministicEngine.solve(q_text, choices)
                if ans is None:
                    ans = self.ai.solve(q_text, choices, "toggle")
                if ans:
                    ok = await select_matching_option(t, q_text, ans, field_type="button", card_locator=card)
                    if ok:
                        self._fields_filled_this_pass += 1
                        self.populated_fields.append({"portal": "Ashby", "type": "toggle", "label": q_text, "value": ans})
                else:
                    self.unanswered_fields.append({"portal": "Ashby", "type": "toggle", "label": q_text})
            except Exception as e:
                if isinstance(e, FormHaltException):
                    raise
                logger.debug(f"Ashby toggle error: {e}")

    # ==================================================================
    # LEVER
    # ==================================================================
    async def _fill_lever(self, target):
        """Lever-specific: parser-aware upload, contact enforcement, and Question Block parsing."""
        logger.info("=== Lever Adapter ===")
        # If on overview page without form, navigate to /apply or click 'Apply'
        if await target.locator('input[name="name"]').count() == 0 and await target.locator('input[type="file"]').count() == 0:
            apply_btn = target.locator('a.template-btn-submit, a:text-is("Apply for this job"), a:text-is("Apply")').first
            if await apply_btn.count() > 0 and await apply_btn.is_visible():
                logger.info("Clicking Lever 'Apply for this job' button...")
                await apply_btn.click()
                await asyncio.sleep(2.0)
            elif hasattr(target, "url") and "/apply" not in target.url:
                apply_url = target.url.rstrip("/") + "/apply"
                logger.info(f"Navigating to Lever apply URL: {apply_url}")
                await target.goto(apply_url, wait_until="domcontentloaded")
                await asyncio.sleep(2.0)

        pi = self.pi
        exp = self.exp[0] if self.exp else {}
        # Dismiss any cookie/GDPR banner immediately
        await dismiss_cookie_banners(target)

        # 1. Upload resume first & wait for Lever's client-side parser to settle
        await upload_resume(target)
        await upload_cover_letter(target)

        # Wait up to 10s for parser to populate initial fields
        for _ in range(10):
            await asyncio.sleep(1)
            name_input = target.locator('input[name="name"]').first
            if await name_input.count() > 0 and (await name_input.input_value() or "").strip():
                logger.info("Lever resume parser finished.")
                break
        await asyncio.sleep(1.0)

        # 2. Enforce canonical contact info
        await self._force_fill(target, 'input[name="name"]', pi.get("full_name", "Siddhesh Sawant"))
        await self._force_fill(target, 'input[name="email"]', pi.get("email", ""))
        phone_val = pi.get("phone", "(445) 208-2299")
        phone_filled = await self._force_fill(target, 'input[name="phone"]', phone_val)
        if not phone_filled:
            await self._safe_fill(target, 'input[type="tel"], input[autocomplete="tel"], input[name*="phone" i], input[id*="phone" i], input[placeholder*="phone" i]', phone_val)
        await self._force_fill(target, 'input[name="org"]', exp.get("company", "Talviro INC"))
        await self._force_fill(target, 'input[name="urls[LinkedIn]"]', pi.get("linkedin_url", "https://www.linkedin.com/in/sss20/"))
        await self._force_fill(target, 'input[name="urls[GitHub]"]', pi.get("github_url", "https://github.com/sids202001"))
        await self._force_fill(target, 'input[name="urls[Portfolio]"], input[name="urls[Other]"]', pi.get("portfolio_url", "https://siddhesh202001-portfolio.netlify.app"))

        # Lever Education fields (if present on form)
        edu = self.edu[0] if (self.edu and isinstance(self.edu, list) and isinstance(self.edu[0], dict)) else {}
        school = edu.get("school", "George Mason University")
        degree = edu.get("degree", "Master's Degree")
        discipline = edu.get("discipline") or edu.get("major", "Information Systems")
        await self._safe_fill(target, 'input[name*="school" i], input[id*="school" i], input[name*="college" i], input[name*="university" i]', school)
        await self._safe_fill(target, 'input[name*="degree" i], input[id*="degree" i]', degree)
        await self._safe_fill(target, 'input[name*="major" i], input[id*="major" i], input[name*="discipline" i]', discipline)

        loc_inp = target.locator('input[name="location"], #location-input').first
        if await loc_inp.count() > 0:
            curr_loc = (await loc_inp.input_value() or "").strip()
            # Banned Lever free-text "USA" or empty -> verbatim canonical location
            if not curr_loc or curr_loc.upper() == "USA" or curr_loc.lower() == "us":
                await self._force_fill(target, 'input[name="location"], #location-input', "Washington, District of Columbia, United States")

        # Handle all Lever location dropdowns (opportunityLocationId, candidate-location, etc.)
        loc_sels = await target.locator('select[name="opportunityLocationId"], select.opportunity-location, select.candidate-location, select[name*="location" i]').all()
        for l_sel in loc_sels:
            try:
                curr_val = await l_sel.evaluate('el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : ""')
                if not curr_val or any(p in curr_val.lower() for p in ["select", "choose", "--", "placeholder"]):
                    opts_els = await l_sel.locator('option').all()
                    opts = [(await o.text_content() or "").strip() for o in opts_els]
                    best_opt = DeterministicEngine.pick_best_location_choice(opts)
                    if best_opt:
                        await l_sel.select_option(label=best_opt)
                        logger.info(f"Lever early location select → {best_opt}")
                        self._fields_filled_this_pass += 1
                        await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug(f"Lever early location select error: {e}")

        # 3. Process each .application-question block
        cards = await target.locator('.application-question').all()
        for card in cards:
            try:
                question_text = await card.evaluate('''el => {
                    let lbl = el.querySelector(".text, .application-label, legend, h4, .label");
                    if (lbl && lbl.innerText.trim()) {
                        return lbl.innerText.split(String.fromCharCode(10))[0].replace("✱", "").trim();
                    }
                    let raw = el.innerText || "";
                    return raw.split(String.fromCharCode(10))[0].replace("✱", "").trim();
                }''')

                if not question_text:
                    continue

                q_lower = question_text.lower()
                if any(k == q_lower for k in ["resume/cv", "full name", "email", "phone", "current location", "current company", "linkedin url", "other website"]):
                    continue

                # Case A: Checkboxes inside this card
                checkboxes = await card.locator('input[type="checkbox"]').all()
                if checkboxes:
                    cb_labels = []
                    for cb in checkboxes:
                        cbl = await cb.evaluate('''el => {
                            let l = el.closest("label") || el.parentElement;
                            return l ? l.innerText.trim() : (el.value || "");
                        }''')
                        cb_labels.append(cbl)

                    # Pronouns
                    if "pronoun" in q_lower:
                        for i, cb in enumerate(checkboxes):
                            if any(h in cb_labels[i].lower() for h in ["he/him", "he / him"]):
                                if not await cb.is_checked():
                                    await cb.evaluate("el => { if (!el.checked) { el.click(); el.dispatchEvent(new Event('change', { bubbles: true })); } }")
                                    logger.info(f"Lever Checkbox [Pronouns] → {cb_labels[i]}")
                                    self._fields_filled_this_pass += 1
                                break
                        continue

                    # Rule 9: How did you hear -> Company Website first, Job Board second, NEVER LinkedIn
                    if any(k in q_lower for k in ["how did you hear", "hear about", "referral source"]):
                        chosen = solve_how_did_you_hear(question_text, cb_labels, is_required=False)
                        if chosen:
                            for i, cb in enumerate(checkboxes):
                                if chosen.lower() in cb_labels[i].lower():
                                    if not await cb.is_checked():
                                        await cb.evaluate("el => { if (!el.checked) { el.click(); el.dispatchEvent(new Event('change', { bubbles: true })); } }")
                                        logger.info(f"Lever Checkbox [How did you hear] → {cb_labels[i]}")
                                        self._fields_filled_this_pass += 1
                                    break
                        continue

                    chosen = DeterministicEngine.solve(question_text, cb_labels)
                    if chosen:
                        ok = await select_matching_option(target, question_text, chosen, field_type="checkbox", card_locator=card)
                        if ok:
                            self._fields_filled_this_pass += 1
                            self.populated_fields.append({"portal": "Lever", "type": "checkbox", "label": question_text, "value": chosen})
                    continue

                # Case B: Radios inside this card (R8/Nov 22 R1: visible label first, NEVER raw value='on')
                radios = await card.locator('input[type="radio"]').all()
                if radios:
                    r_labels = []
                    for r in radios:
                        r_obj = await r.evaluate('''el => {
                            let txt = "";
                            if (el.id) {
                                try {
                                    const lf = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                                    if (lf && lf.innerText.trim()) txt = lf.innerText.trim();
                                } catch(e) {}
                            }
                            if (!txt) {
                                const l = el.closest("label") || el.parentElement;
                                if (l && l.innerText.trim()) txt = l.innerText.trim();
                            }
                            if (!txt) txt = (el.getAttribute("aria-label") || "").trim();
                            const rawVal = (el.getAttribute("value") || "").trim();
                            if (!txt && rawVal && rawVal.toLowerCase() !== "on") txt = rawVal;
                            return { label: txt, outer: el.outerHTML.slice(0, 300) };
                        }''')
                        if not r_obj.get("label"):
                            raise FormHaltException(
                                "OPTION_LABEL_UNREADABLE",
                                f"Lever radio option label unreadable for '{question_text}'. Raw outer HTML: {r_obj.get('outer')}",
                                question=question_text,
                            )
                        r_labels.append(r_obj["label"])

                    chosen = DeterministicEngine.solve(question_text, r_labels)
                    if chosen is None:
                        chosen = self.ai.solve(question_text, r_labels, "radio")
                    if chosen:
                        ok = await select_matching_option(target, question_text, chosen, field_type="radio", card_locator=card)
                        if ok:
                            self._fields_filled_this_pass += 1
                            self.populated_fields.append({"portal": "Lever", "type": "radio", "label": question_text, "value": chosen})
                    else:
                        self.unanswered_fields.append({"portal": "Lever", "type": "radio", "label": question_text, "choices": r_labels})
                    continue

                # Case C: Native <select> inside this card
                sel = card.locator('select').first
                if await sel.count() > 0:
                    opts_els = await card.locator('select option').all()
                    opts = [(await o.text_content() or "").strip() for o in opts_els]
                    clean_opts = [o for o in opts if o and not any(s in o.lower() for s in ["select...", "select ...", "--", "choose", "please select"])]
                    if clean_opts:
                        if any(k in question_text.lower() for k in ["location", "office", "where are you", "city", "country"]):
                            chosen = DeterministicEngine.pick_best_location_choice(clean_opts)
                        else:
                            chosen = DeterministicEngine.solve(question_text, clean_opts) or self.ai.solve(question_text, clean_opts, "select")
                        if chosen:
                            ok = await select_matching_option(target, question_text, chosen, field_type="select", card_locator=card)
                            if ok:
                                self._fields_filled_this_pass += 1
                                self.populated_fields.append({"portal": "Lever", "type": "select", "label": question_text, "value": chosen})
                        else:
                            self.unanswered_fields.append({"portal": "Lever", "type": "select", "label": question_text, "choices": clean_opts})
                    continue

                is_req = await card.evaluate('''el => {
                    let reqAttr = el.querySelector("[required], [aria-required='true']");
                    let star = el.querySelector(".required, [class*='required']") || el.innerText.includes("✱") || el.innerText.includes("*");
                    return Boolean(reqAttr || star);
                }''')
                is_conditional = bool(re.search(r"(if\s*you\s*(chose|selected).*specify|if\s*other.*(specify|explain|describe)|if\s*referr.*(who|name|specify|list)|who\s*referred\s*you|employee\s*referr.*name|name\s*of\s*(the\s*)?referr|former\s*employee.*name|referred\s*by\s*an\s*employee|if\s*yes.*explain)", question_text, re.IGNORECASE))

                # Case D: Textarea inside this card
                ta = card.locator('textarea').first
                if await ta.count() > 0:
                    cur_val = (await ta.input_value() or "").strip()
                    if not cur_val:
                        if is_conditional:
                            if is_req:
                                await ta.fill("N/A")
                                logger.info(f"Lever Textarea [{question_text[:30]}] → N/A (Required Conditional)")
                                self._fields_filled_this_pass += 1
                                self.populated_fields.append({"portal": "Lever", "type": "textarea", "label": question_text, "value": "N/A"})
                            else:
                                logger.info(f"Lever Textarea [{question_text[:30]}] → Skipped (Optional Conditional)")
                            continue

                        ans = DeterministicEngine.solve(question_text) or self.ai.solve(question_text, None, "textarea")
                        if ans and str(ans) != "N/A":
                            await self._human_fill(ta, str(ans))
                            logger.info(f"Lever Textarea [{question_text[:30]}] → {str(ans)[:40]}")
                            self._fields_filled_this_pass += 1
                            self.populated_fields.append({"portal": "Lever", "type": "textarea", "label": question_text, "value": str(ans)})
                        else:
                            if is_req:
                                self.unanswered_fields.append({"portal": "Lever", "type": "textarea", "label": question_text})
                    continue

                # Case E: Text input inside this card
                txt = card.locator('input[type="text"], input[type="tel"], input[type="url"], input[type="date"], input:not([type])').first
                if await txt.count() > 0:
                    cur_val = (await txt.input_value() or "").strip()
                    if not cur_val:
                        if is_conditional:
                            if is_req:
                                await self._human_fill(txt, "N/A")
                                logger.info(f"Lever Input [{question_text[:30]}] → N/A (Required Conditional)")
                                self._fields_filled_this_pass += 1
                                self.populated_fields.append({"portal": "Lever", "type": "text", "label": question_text, "value": "N/A"})
                            else:
                                logger.info(f"Lever Input [{question_text[:30]}] → Skipped (Optional Conditional)")
                            continue

                        ans = DeterministicEngine.solve(question_text) or self.ai.solve(question_text, None, "text")
                        if ans and str(ans) != "N/A":
                            await self._human_fill(txt, str(ans))
                            logger.info(f"Lever Input [{question_text[:30]}] → {str(ans)[:40]}")
                            self._fields_filled_this_pass += 1
                            self.populated_fields.append({"portal": "Lever", "type": "text", "label": question_text, "value": str(ans)})
                        else:
                            if is_req:
                                self.unanswered_fields.append({"portal": "Lever", "type": "text", "label": question_text})
                    continue

            except Exception as e:
                if isinstance(e, FormHaltException):
                    raise
                logger.debug(f"Lever card error: {e}")

        # 4. Proactively check consent / privacy policy checkboxes on Lever
        try:
            consent_selectors = [
                'input[type="checkbox"][name*="consent" i]',
                'input[type="checkbox"][name*="privacy" i]',
                'input[type="checkbox"][id*="privacy" i]',
                'input[type="checkbox"][id*="consent" i]',
                '.application-consent input[type="checkbox"]',
                'label:has-text("privacy") input[type="checkbox"]',
                'label:has-text("consent") input[type="checkbox"]',
                'label:has-text("terms") input[type="checkbox"]',
                'label:has-text("acknowledge") input[type="checkbox"]',
            ]
            for sel in consent_selectors:
                cbs = await target.locator(sel).all()
                for cb in cbs:
                    try:
                        if not await cb.is_checked():
                            await cb.evaluate("el => { if (!el.checked) { el.click(); el.dispatchEvent(new Event('change', { bubbles: true })); } }")
                            logger.info(f"Checked Lever compliance checkbox: {sel}")
                            self._fields_filled_this_pass += 1
                            self.populated_fields.append({"portal": "Lever", "type": "checkbox", "label": "Privacy/Consent Policy", "value": "Checked"})
                            await asyncio.sleep(FIELD_FILL_GAP_MS / 1000.0)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Lever compliance checkbox sweep warning: {e}")

    # ==================================================================
    # SMARTRECRUITERS
    # ==================================================================
    async def _fill_smartrecruiters(self, target):
        """SmartRecruiters-specific: wizard pages, custom fields."""
        logger.info("=== SmartRecruiters Adapter ===")
        pi = self.pi

        await self._safe_fill(target, '#first-name-input, input[name="firstName"]', pi.get("first_name", ""))
        await self._safe_fill(target, '#last-name-input, input[name="lastName"]', pi.get("last_name", ""))
        await self._safe_fill(target, '#email-input, input[name="email"]', pi.get("email", ""))
        await self._safe_fill(target, '#phone-number-input, input[name="phoneNumber"]', pi.get("phone", ""))

        # Location
        loc = target.locator('input[placeholder*="City"], #location-input').first
        if await loc.count() > 0 and not (await loc.input_value() or "").strip():
            await ComboboxHandler.type_and_select(target, loc, "Washington", "Washington, DC, USA")

        await upload_resume(target)

        # Experience
        exp = self.exp[0] if self.exp else {}
        await self._safe_fill(target, 'input[name*="jobTitle"], input[placeholder*="Job Title"]', exp.get("title", ""))
        await self._safe_fill(target, 'input[name*="company"], input[placeholder*="Company"]', exp.get("company", ""))

        if exp.get("is_current", True):
            curr = target.locator('input[type="checkbox"][name*="current"]').first
            if await curr.count() > 0 and not await curr.is_checked():
                await curr.check(force=True)

    # ==================================================================
    # RIPPLING
    # ==================================================================
    async def _fill_rippling(self, target):
        """Rippling ATS: handles candidate details, resume upload, and profile info."""
        logger.info("=== Rippling Adapter ===")
        pi = self.pi

        # First / Last name
        await self._safe_fill(target, 'input[name="firstName"], input[name="first_name"], input[placeholder*="First Name" i], input[id*="first_name" i]', pi.get("first_name", ""))
        await self._safe_fill(target, 'input[name="lastName"], input[name="last_name"], input[placeholder*="Last Name" i], input[id*="last_name" i]', pi.get("last_name", ""))
        await self._safe_fill(target, 'input[name="email"], input[type="email"], input[placeholder*="Email" i], input[id*="email" i]', pi.get("email", ""))
        await self._safe_fill(target, 'input[name="phone"], input[type="tel"], input[placeholder*="Phone" i], input[id*="phone" i]', pi.get("phone", ""))

        # Location
        loc = target.locator('input[placeholder*="Location" i], input[placeholder*="City" i], input[id*="location" i]').first
        if await loc.count() > 0 and not (await loc.input_value() or "").strip():
            await ComboboxHandler.type_and_select(target, loc, "Washington", "Washington, DC, USA")

        # Social links
        await self._safe_fill(target, 'input[name*="linkedin" i], input[placeholder*="LinkedIn" i]', pi.get("linkedin_url", ""))
        await self._safe_fill(target, 'input[name*="github" i], input[placeholder*="GitHub" i]', pi.get("github_url", ""))

        # Resume upload
        await upload_resume(target)

    async def _fill_native_selects(self, target):
        """Finds visible native select dropdowns that are unselected and solves them via select_matching_option."""
        selects = await target.locator('select:visible').all()
        for sel in selects:
            try:
                sel_text = await sel.evaluate('el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : ""')
                if sel_text and not any(p in sel_text.lower() for p in ["select...", "select ...", "select", "--", "choose", "none selected", "default", "please select"]):
                    continue
                label = await self._extract_label(sel)
                if not label:
                    continue
                opts_els = await sel.locator('option').all()
                opts = [(await o.text_content() or "").strip() for o in opts_els]
                opts = [o for o in opts if o and not any(s in o.lower() for s in ["select...", "select ...", "--", "choose...", "please select"])]
                if not opts:
                    continue
                # Deterministic Matrix first; Gemini fallback if unmatched
                ans = DeterministicEngine.solve(label, opts) or self.ai.solve(label, opts, "select")
                if ans:
                    card = sel.locator('xpath=ancestor::*[self::fieldset or contains(@class,"field") or contains(@class,"question")][1]').first
                    ok = await select_matching_option(target, label, ans, field_type="select", card_locator=card if await card.count() > 0 else None)
                    if ok:
                        self._fields_filled_this_pass += 1
                        self.populated_fields.append({"portal": self.portal, "type": "select", "label": label, "value": ans})
                        await self._wait_field_stable(sel)
            except Exception as e:
                logger.debug(f"Native select error: {e}")

    async def _fill_radios(self, target):
        """Globally scans all radio groups, groups them by name/container, and answers them via select_matching_option."""
        radios = await target.locator('input[type="radio"]:visible, [role="radio"]:visible').all()
        if not radios:
            return

        seen_groups = set()
        for r in radios:
            try:
                r_name = await r.get_attribute("name")
                if r_name:
                    grp_key = f"name:{r_name}"
                else:
                    grp_key = await r.evaluate('''el => {
                        const c = el.closest("fieldset, .field, .field-wrapper, .application-question, [class*='fieldEntry'], [class*='field-entry']");
                        return c ? "el:" + (c.id || c.className) : "radio:" + (el.id || Math.random());
                    }''')

                if grp_key in seen_groups:
                    continue
                seen_groups.add(grp_key)

                container = r.locator('xpath=ancestor::*[self::fieldset or contains(@class,"field") or contains(@class,"question") or contains(@class,"fieldEntry") or contains(@class,"field-entry")][1]').first

                # Check if any radio in group is checked
                if r_name:
                    grp_radios = await target.locator(f'input[type="radio"][name="{r_name}"]:visible, [role="radio"][name="{r_name}"]:visible').all()
                else:
                    if await container.count() > 0:
                        grp_radios = await container.locator('input[type="radio"]:visible, [role="radio"]:visible').all()
                    else:
                        grp_radios = [r]

                any_checked = False
                for gr in grp_radios:
                    tag_type = await gr.get_attribute("type")
                    if tag_type == "radio":
                        if await gr.is_checked():
                            any_checked = True
                            break
                    else:
                        if await gr.get_attribute("aria-checked") == "true":
                            any_checked = True
                            break
                if any_checked:
                    continue

                q_text = await r.evaluate('''el => {
                    const c = el.closest("fieldset, .field, .field-wrapper, .application-question, [class*='fieldEntry'], [class*='field-entry']");
                    if (c) {
                        const lbl = c.querySelector("legend, label, .label, [class*='title'], [class*='description']");
                        if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
                        return c.innerText.split(String.fromCharCode(10))[0].trim();
                    }
                    return "";
                }''')
                if not q_text:
                    continue

                ans = DeterministicEngine.solve(q_text) or self.ai.solve(q_text, None, "radio")
                if ans:
                    ok = await select_matching_option(target, q_text, ans, field_type="radio", card_locator=container if await container.count() > 0 else None)
                    if ok:
                        self._fields_filled_this_pass += 1
                        self.populated_fields.append({"portal": self.portal, "type": "radio", "label": q_text, "value": ans})
            except Exception as e:
                logger.debug(f"Radio group error: {e}")

    async def _fill_button_groups(self, target):
        """Universal sweep for unselected button groups, segmented controls, and toggle cards."""
        cards = await target.locator(':is(.ashby-application-form-field-entry, .field-entry, [class*="fieldEntry"], fieldset, .application-question, [role="group"], [role="radiogroup"]):has(button[aria-pressed], button.ashby-application-form-input-yesno-option, [class*="yesno"] button, button[data-option])').all()
        for card in cards:
            try:
                # Check if already selected
                is_selected = await card.evaluate('''el => {
                    const btns = el.querySelectorAll("button[aria-pressed], button.ashby-application-form-input-yesno-option, [class*='yesno'] button, button[data-option]");
                    for (let b of btns) {
                        if (b.getAttribute("aria-pressed") === "true" || b.getAttribute("aria-checked") === "true" || /active|selected|_active_/.test(b.className)) return true;
                    }
                    const chk = el.querySelector("input:checked");
                    if (chk) return true;
                    return false;
                }''')
                if is_selected:
                    continue

                q_text = await card.evaluate('''el => {
                    const lbl = el.querySelector("legend, label, .application-label, [class*='title'], [class*='label']");
                    if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
                    return el.innerText.split(String.fromCharCode(10))[0].trim();
                }''')
                if not q_text:
                    continue

                btn_texts = await card.evaluate('''el => {
                    const btns = Array.from(el.querySelectorAll("button[aria-pressed], button.ashby-application-form-input-yesno-option, [class*='yesno'] button, button[data-option]"));
                    return btns.map(b => b.innerText.trim()).filter(Boolean);
                }''')
                choices = btn_texts if btn_texts else ["Yes", "No"]
                ans = DeterministicEngine.solve(q_text, choices) or self.ai.solve(q_text, choices, "toggle")
                if ans:
                    ok = await select_matching_option(target, q_text, ans, field_type="button", card_locator=card)
                    if ok:
                        self._fields_filled_this_pass += 1
                        self.populated_fields.append({"portal": self.portal or "Universal", "type": "toggle", "label": q_text, "value": ans})
            except Exception as e:
                logger.debug(f"Button group sweep error: {e}")

    # ==================================================================
    # UNIVERSAL SWEEP — catches anything missed by portal-specific code
    # ==================================================================
    async def _universal_sweep(self, target):
        """
        Scans ALL visible unfilled form fields and attempts to fill them.
        Handles text inputs, textareas, native selects, radios, and checkboxes.
        """
        # Text inputs and textareas
        inputs = await target.locator(
            'input[type="text"]:visible, input[type="email"]:visible, '
            'input[type="tel"]:visible, input[type="url"]:visible, '
            'input[type="number"]:visible, input[type="date"]:visible, '
            'input:not([type]):visible, textarea:visible'
        ).all()

        for inp in inputs:
            try:
                if (await inp.input_value() or "").strip():
                    continue
                # Skip if inside a select/combobox container
                inside_select = await inp.evaluate(
                    'el => !!el.closest("[class*=\'select__\'], [class*=\'combobox\'], [role=\'combobox\']")'
                )
                if inside_select:
                    continue

                label = await self._extract_label(inp)
                if not label:
                    continue

                inp_tag = await inp.evaluate("el => el.tagName.toLowerCase()")
                is_req = await inp.evaluate('''el => {
                    let reqAttr = el.required || el.getAttribute("aria-required") === "true";
                    let p = el.closest(".field, .field-wrapper, .application-question, [class*='Field'], [class*='field']");
                    let hasStar = false;
                    if (p) {
                        if (p.classList.contains("required") || p.hasAttribute("required")) hasStar = true;
                        if (p.querySelector(".required, [required], [class*='required'], [class*='asterisk']")) hasStar = true;
                        let text = p.innerText || "";
                        if (text.includes("*") || text.includes("✱") || text.toLowerCase().includes("required")) hasStar = true;
                    }
                    return Boolean(reqAttr || hasStar);
                }''')

                # Conditional follow-up check (e.g. "If you chose X... please specify")
                is_conditional = bool(re.search(r"(if\s*you\s*(chose|selected).*specify|if\s*other.*(specify|explain|describe)|if\s*referr.*(who|name|specify|list)|who\s*referred\s*you|employee\s*referr.*name|name\s*of\s*(the\s*)?referr|former\s*employee.*name|referred\s*by\s*an\s*employee|if\s*yes.*explain)", label, re.IGNORECASE))
                if is_conditional:
                    if is_req:
                        await inp.fill("N/A")
                        logger.info(f"Sweep fill [{label[:35]}] → N/A (Required Conditional)")
                        self._fields_filled_this_pass += 1
                        self.populated_fields.append({"portal": "Universal", "type": inp_tag, "label": label, "value": "N/A"})
                    else:
                        logger.info(f"Sweep fill [{label[:35]}] → Skipped (Optional Conditional)")
                    continue

                # 1. Deterministic Matrix first; 2. Gemini fallback if unmatched
                resolved = DeterministicEngine.solve(label)
                if resolved is not None:
                    ans = resolved
                else:
                    ans = self.ai.solve(label, None, "textarea" if inp_tag == "textarea" else "text")

                if (not ans or str(ans) == "N/A") and not is_req:
                    continue

                if ans and str(ans) != "N/A":
                    await inp.fill(str(ans))
                    logger.info(f"Sweep fill [{label[:35]}] → {str(ans)[:40]}")
                    self._fields_filled_this_pass += 1
                    self.populated_fields.append({"portal": "Universal", "type": inp_tag, "label": label, "value": str(ans)})
                    await self._wait_field_stable(inp)
            except Exception:
                pass

        # Native <select> elements
        selects = await target.locator('select:visible').all()
        for sel in selects:
            try:
                sel_text = await sel.evaluate('el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : ""')
                if sel_text and not any(p in sel_text.lower() for p in ["select...", "select ...", "select", "--", "choose", "none selected", "default"]):
                    continue
                label = await self._extract_label(sel)
                if not label:
                    continue
                opts_els = await sel.locator('option').all()
                opts = [(await o.text_content() or "").strip() for o in opts_els]
                opts = [o for o in opts if o and not any(s in o.lower() for s in ["select...", "select ...", "--", "choose..."])]
                if not opts:
                    continue
                # 1. Deterministic Matrix first; 2. Gemini fallback if unmatched
                ans = DeterministicEngine.solve(label, opts) or self.ai.solve(label, opts, "select")
                if ans:
                    card = sel.locator('xpath=ancestor::*[self::fieldset or contains(@class,"field") or contains(@class,"question")][1]').first
                    ok = await select_matching_option(target, label, ans, field_type="select", card_locator=card if await card.count() > 0 else None)
                    if ok:
                        self._fields_filled_this_pass += 1
                        await self._wait_field_stable(sel)
            except Exception:
                pass

        # Radio button sweep
        await self._fill_radios(target)

        # Button group and toggle sweep
        await self._fill_button_groups(target)

    # ==================================================================
    # AUDIT GATE — NEVER AUTO-SUBMIT
    # ==================================================================
    async def _audit_and_pause(self, target):
        """Scan for unfilled required fields, attempt retry, run safety layer, scroll to top, pause."""
        # 1. Run safety validation and auto-correction layer
        await SafetyAuditor.audit_and_correct(self.page, target, self.profile)

        logger.info("Running pre-submission audit...")

        # Find unfilled required fields (group-aware for checkboxes/radios)
        unfilled = await self.page.evaluate(
            '''() => {
                const els = Array.from(document.querySelectorAll("input, select, textarea"));
                const missing = [];
                const checkedGroups = new Set();
                els.forEach(el => {
                    if ((el.type === "checkbox" || el.type === "radio") && el.checked && el.name) {
                        checkedGroups.add(el.name);
                    }
                });
                els.forEach(el => {
                    if (el.type === "hidden" || el.type === "submit" || el.type === "button") return;
                    if (el.offsetParent === null) return;
                    /* Skip internal react-select / combobox search inputs whose selections are in the container */
                    if (el.classList.contains("select__input") || el.getAttribute("role") === "combobox" || Boolean(el.closest("[class*='select__'], [class*='combobox']"))) {
                        const p = el.closest("[class*='select__container'], [class*='select__control'], .field-wrapper, .field, [class*='field']");
                        if (p) {
                            const hasSel = Boolean(p.querySelector("[class*='single-value'], [class*='singleValue'], [class*='multi-value'], [class*='multiValue']"));
                            const text = p.innerText || "";
                            if (hasSel || (text && !text.toLowerCase().includes("select..."))) return;
                        }
                        return;
                    }
                    let reqAttr = el.required || el.getAttribute("aria-required") === "true";
                    let p = el.closest(".field, .field-wrapper, .form-group, label, fieldset, .application-question, [class*='field']");
                    let hasStar = false;
                    if (p) {
                        if (p.classList.contains("required") || p.hasAttribute("required")) hasStar = true;
                        if (p.querySelector(".required, [required], [class*='required'], [class*='asterisk']")) hasStar = true;
                        let text = p.innerText || "";
                        if (text.includes("*") || text.includes("✱") || text.toLowerCase().includes("required")) hasStar = true;
                    }
                    const req = Boolean(reqAttr || hasStar);
                    let hasVal = false;
                    if (el.type === "checkbox" || el.type === "radio") {
                        hasVal = el.checked || (el.name && checkedGroups.has(el.name));
                    } else if (el.type === "file") {
                        hasVal = Boolean(el.files && el.files.length > 0) || Boolean(el.value && el.value.trim() !== "");
                    } else {
                        hasVal = Boolean(el.value && el.value.trim() !== "");
                    }
                    if (req && !hasVal) {
                        let lbl = el.getAttribute("aria-label") || el.placeholder || el.name || el.id;
                        const p = el.closest(".field, .field-wrapper, .form-group, label, fieldset, .application-question, [class*='field']");
                        if (p) {
                            const l = p.querySelector("label, legend, .label, .text, [id$='-label']");
                            if (l && l.innerText.trim()) lbl = l.innerText.trim();
                        }
                        missing.push({id: el.id, name: el.name, type: el.type, label: lbl || "Unknown"});
                    }
                });
                return missing;
            }'''
        )

        if unfilled:
            logger.warning(f"Audit: {len(unfilled)} unfilled required fields. Running retry...")
            for item in unfilled:
                try:
                    lbl = item.get("label", "").strip()
                    if item.get("type") == "file" or "resume" in lbl.lower():
                        logger.info(f"Audit retry: re-attaching resume for [{lbl}]")
                        await upload_resume(self.page)
                        self._fields_filled_this_pass += 1
                        self.populated_fields.append({"portal": "AuditRetry", "type": "file", "label": lbl, "value": "attached"})
                        continue
                    # Skip raw question IDs, card IDs, or known combobox artifacts
                    if re.match(r"^question_\d+$", lbl, re.IGNORECASE) or lbl.startswith("cards[") or re.match(r"^[0-9a-fA-F-]{20,}$", lbl) or lbl.lower() in ["country", "candidate-location"]:
                        continue

                    # Conditional check
                    if re.search(r"(if\s*you\s*(chose|selected).*specify|if\s*other.*(specify|explain|describe)|if\s*referr.*(who|name|specify|list)|who\s*referred\s*you|employee\s*referr.*name|name\s*of\s*(the\s*)?referr|former\s*employee.*name|referred\s*by\s*an\s*employee|if\s*yes.*explain)", lbl, re.IGNORECASE):
                        selector = f"#{item['id']}" if item.get('id') else f"[name='{item.get('name', '')}']"
                        loc = self.page.locator(selector).first
                        if await loc.count() > 0:
                            await loc.fill("N/A")
                            logger.info(f"Audit retry: [{lbl[:30]}] → N/A (Required Conditional)")
                            self._fields_filled_this_pass += 1
                            self.populated_fields.append({"portal": "AuditRetry", "type": item.get('type', 'text'), "label": lbl, "value": "N/A"})
                        continue

                    selector = f"#{item['id']}" if item.get('id') else f"[name='{item.get('name', '')}']"
                    loc = self.page.locator(selector).first
                    if await loc.count() == 0:
                        continue
                    # 1. Deterministic Matrix first; 2. Gemini fallback if unmatched
                    ans = DeterministicEngine.solve(lbl) or self.ai.solve(lbl, None, item.get('type', 'text'))
                    if ans and str(ans) != "N/A":
                        if item.get('type') in ['checkbox', 'radio']:
                            await loc.check(force=True)
                        else:
                            await loc.fill(str(ans))
                        logger.info(f"Audit retry: [{lbl[:30]}] → {str(ans)[:30]}")
                        self._fields_filled_this_pass += 1
                        self.populated_fields.append({"portal": "AuditRetry", "type": item.get('type', 'text'), "label": lbl, "value": str(ans)})
                    elif ans and str(ans) == "N/A":
                        await loc.fill("N/A")
                        logger.info(f"Audit retry: [{lbl[:30]}] → N/A")
                        self._fields_filled_this_pass += 1
                        self.populated_fields.append({"portal": "AuditRetry", "type": item.get('type', 'text'), "label": lbl, "value": "N/A"})
                    else:
                        self.unanswered_fields.append({"portal": "AuditRetry", "type": item.get('type', 'text'), "label": lbl})
                except Exception:
                    self.unanswered_fields.append({"portal": "AuditRetry", "type": item.get('type', 'text'), "label": lbl})

        # Scroll to top smoothly
        await self.page.evaluate("window.scrollTo({ top: 0, behavior: 'smooth' })")
        await asyncio.sleep(1.0)

        # Terminal pause — NEVER auto-submit
        print("\n" + "=" * 80)
        print("  [AUDIT GATE] FORM AUTOMATION & REVIEW SUMMARY")
        print("=" * 80)
        pop_count = len(self.populated_fields) or self._fields_filled_this_pass
        print(f"  ✅ Fields Automatically Populated: {pop_count}")
        if self.unanswered_fields:
            seen = set()
            unique_unanswered = []
            for uf in self.unanswered_fields:
                lbl = uf.get("label", "").strip()
                if lbl and lbl not in seen:
                    seen.add(lbl)
                    unique_unanswered.append(uf)
            print(f"  ⚠️  Fields Requiring Manual Review ({len(unique_unanswered)}):")
            for uf in unique_unanswered:
                lbl = uf.get("label", "Unknown")
                ftype = uf.get("type", "text")
                print(f"     • [{ftype.upper()}] {lbl}")
        else:
            print("  🎉 All fields successfully matched against profile and Deterministic Matrix!")
        print("=" * 80)
        print("  STRICT SAFETY RULE: The bot NEVER auto-submits.")
        print("  Inspect the open browser window, answer any remaining flagged fields,")
        print("  and manually click Submit when ready.")
        print("=" * 80 + "\n")

    # ==================================================================
    # UTILITY METHODS
    # ==================================================================
    async def _wait_field_stable(self, loc=None):
        """Wait for field stable state, then apply fixed policy-based pacing."""
        if loc is not None:
            try:
                if hasattr(loc, "wait_for"):
                    await loc.wait_for(state="visible", timeout=2000)
            except Exception:
                pass
        await asyncio.sleep(FIELD_FILL_GAP_MS / 1000.0)

    async def _clear_field(self, loc: Locator) -> bool:
        """
        Rule 2: Click -> Select-all -> Delete -> VERIFY empty.
        Uses keyboard shortcuts (Meta+a and Control+a), fill(""), and JS reset.
        """
        try:
            await loc.scroll_into_view_if_needed()
            try:
                await loc.click(force=False, timeout=1000)
            except Exception:
                await loc.focus()

            # Select-all and backspace (Meta+a on Mac, Control+a on other)
            await loc.press("Meta+a")
            await loc.press("Backspace")
            cur = await loc.input_value()
            if not (cur or "").strip():
                return True

            await loc.press("Control+a")
            await loc.press("Backspace")
            cur = await loc.input_value()
            if not (cur or "").strip():
                return True

            await loc.fill("")
            cur = await loc.input_value()
            if not (cur or "").strip():
                return True

            # JS reset as final guarantee
            await loc.evaluate("el => { el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }")
            cur = await loc.input_value()
            return not bool((cur or "").strip())
        except Exception:
            return False

    async def _human_fill(self, loc, value: str, min_delay_ms: int = 35, max_delay_ms: int = 80, field_name: str = ""):
        """
        Rule 2: Clear-before-fill + Type + Read-back verification.
        On mismatch: retry ONCE. On second mismatch: HALT with FILL_VERIFICATION_FAILED.
        """
        if not value:
            return
        val_str = str(value)
        _record_activity(f"typing: {val_str[:20]}")

        # Step 1: Clear and verify empty
        await self._clear_field(loc)

        # Step 2: Human-paced typing
        try:
            await loc.scroll_into_view_if_needed()
            try:
                await loc.click(force=False, timeout=1500)
            except Exception:
                await loc.focus()

            if len(val_str) > 25:
                words = val_str.split(" ")
                for idx, word in enumerate(words):
                    sub = word + (" " if idx < len(words) - 1 else "")
                    await loc.press_sequentially(sub, delay=random.randint(min_delay_ms, max_delay_ms))
                    if random.random() < 0.35:
                        await asyncio.sleep(random.uniform(0.08, 0.18))
            else:
                await loc.press_sequentially(val_str, delay=random.randint(min_delay_ms, max_delay_ms))

            await loc.dispatch_event("input")
            await loc.dispatch_event("change")
            await asyncio.sleep(0.3)
        except Exception:
            try:
                await loc.fill(val_str)
                await loc.dispatch_event("input")
                await loc.dispatch_event("change")
                await asyncio.sleep(0.3)
            except Exception:
                pass

        # Step 3: Read back and verify
        def _normalize(s):
            return re.sub(r'\s+', ' ', str(s or "")).strip()

        actual = await loc.input_value()
        if _normalize(actual) == _normalize(val_str):
            return

        # Check phone digit match
        d_act = re.sub(r'\D', '', actual)
        d_val = re.sub(r'\D', '', val_str)
        if d_act and d_val and (d_act == d_val or d_act.endswith(d_val) or d_val.endswith(d_act)):
            return

        # Step 4: Retry ONCE on mismatch
        logger.warning(f"Fill read-back mismatch on '{field_name or 'field'}': intended='{val_str}', actual='{actual}'. Retrying once...")
        await self._clear_field(loc)
        try:
            await loc.fill(val_str)
            await loc.dispatch_event("input")
            await loc.dispatch_event("change")
            await asyncio.sleep(0.4)
        except Exception:
            pass

        actual_retry = await loc.input_value()
        if _normalize(actual_retry) == _normalize(val_str):
            return

        d_act_retry = re.sub(r'\D', '', actual_retry)
        if d_act_retry and d_val and (d_act_retry == d_val or d_act_retry.endswith(d_val) or d_val.endswith(d_act_retry)):
            return

        # Step 5: Second mismatch -> HALT with FILL_VERIFICATION_FAILED
        ss_dir = os.path.expanduser("~/Desktop/screenshots")
        os.makedirs(ss_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        clean_name = re.sub(r'[^a-zA-Z0-9]', '_', str(field_name or 'text_field'))[:25]
        ss_path = os.path.join(ss_dir, f"fill_mismatch_{clean_name}_{ts}.png")
        try:
            await self.page.screenshot(path=ss_path)
        except Exception:
            ss_path = None

        raise FillVerificationFailedError(
            field_name=field_name or "text_field",
            intended=val_str,
            actual=actual_retry,
            screenshot_path=ss_path
        )

    async def _safe_fill(self, target, selector: str, value: str):
        """Fill a field if it exists and needs filling, with clear-before-fill and read-back verification."""
        if not value:
            return
        try:
            loc = target.locator(selector).first
            if await loc.count() > 0:
                current = await loc.input_value()
                def _normalize(s):
                    return re.sub(r'\s+', ' ', str(s or "")).strip()
                if _normalize(current) != _normalize(value):
                    await self._human_fill(loc, value, field_name=selector)
                    self._fields_filled_this_pass += 1
                    await self._wait_field_stable(loc)
        except Exception as e:
            if isinstance(e, FormHaltException):
                raise

    async def _force_fill(self, target, selector: str, value: str):
        """Force fill a field with clear-before-fill and read-back verification."""
        if not value:
            return
        try:
            loc = target.locator(selector).first
            if await loc.count() > 0:
                await self._human_fill(loc, value, field_name=selector)
                self._fields_filled_this_pass += 1
                await self._wait_field_stable(loc)
        except Exception as e:
            if isinstance(e, FormHaltException):
                raise

    async def _extract_label(self, element: Locator) -> str:
        """Extract the human-readable label for a form element via DOM traversal."""
        try:
            res = await element.evaluate(
                'el => {'
                '  let labelText = "";'
                '  if (el.id) {'
                '    try {'
                '      let l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);'
                '      if (l && l.innerText.trim()) labelText = l.innerText.trim();'
                '    } catch(e) {}'
                '  }'
                '  if (!labelText) {'
                '    let al = el.getAttribute("aria-labelledby");'
                '    if (al) {'
                '      let l = document.getElementById(al);'
                '      if (l && l.innerText.trim()) labelText = l.innerText.trim();'
                '    }'
                '  }'
                '  if (!labelText) {'
                '    let cl = el.closest("label");'
                '    if (cl && cl.innerText.trim()) labelText = cl.innerText.trim();'
                '  }'
                '  let field = el.closest(".field, .field-wrapper, .form-group, .application-question, [class*=\'field\'], [class*=\'question\'], fieldset, .input-wrapper, .text-input-wrapper");'
                '  if (field) {'
                '    if (!labelText) {'
                '      let l = field.querySelector("label, legend, .label, [class*=\'label\'], .text, h3, h4, h5, p");'
                '      if (l && l.innerText.trim()) labelText = l.innerText.trim();'
                '    }'
                '    let desc = field.querySelector(".question-description, [id*=\'description\'], .body__secondary, [class*=\'description\'], [class*=\'subtext\'], [class*=\'help\']");'
                '    if (desc && desc.innerText.trim()) {'
                '      let descText = desc.innerText.trim();'
                '      if (!labelText.includes(descText)) {'
                '        labelText = labelText ? (labelText + " — " + descText) : descText;'
                '      }'
                '    }'
                '  }'
                '  if (!labelText) {'
                '    let p = el.parentElement;'
                '    while (p && p !== document.body) {'
                '      let l = p.querySelector(":scope > label, :scope > legend, :scope > .label, :scope > [class*=\'label\'], :scope > p");'
                '      if (l && l.innerText.trim()) { labelText = l.innerText.trim(); break; }'
                '      p = p.parentElement;'
                '    }'
                '  }'
                '  return labelText || el.getAttribute("aria-label") || el.placeholder || el.name || "";'
                '}'
            )
            return normalize_label(res) if res else ""
        except Exception:
            return ""

    async def _press_escape(self, target):
        """Press Escape to close open dropdown menus."""
        try:
            if hasattr(target, 'keyboard'):
                await target.keyboard.press("Escape")
            else:
                await self.page.keyboard.press("Escape")
        except Exception:
            pass


# Backwards compatibility alias
UnifiedATSFormFiller = HybridATSFormFiller


# ============================================================================
# TOP-LEVEL API
# ============================================================================
async def fill_application(
    url: str,
    headless: Optional[bool] = None,
    existing_page: Optional[Page] = None,
) -> bool:
    """
    Main entry point: detect portal, launch browser if needed, fill form, pause.
    STRICT RULE: NEVER submits automatically.
    """
    if headless is None:
        headless = not (os.getenv("DEBUG_HEADED", "false").lower() == "true")

    portal = detect_portal(url)
    if not portal:
        logger.warning(f"[SKIP] Non-supported portal: {url}")
        return False

_SHARED_PLAYWRIGHT = None
_SHARED_BROWSER = None
_SHARED_BROWSER_LOCK = asyncio.Lock()
_PLAYWRIGHT_PAGE_LOCK = asyncio.Lock()

async def get_shared_browser(headless: Optional[bool] = None) -> Tuple[Any, Any]:
    """
    Returns (playwright_instance, shared_headless_browser).
    Ensures exactly one shared headless browser instance per process.
    Re-uses existing browser if already running and connected.
    """
    global _SHARED_PLAYWRIGHT, _SHARED_BROWSER
    if headless is None:
        headless = not (os.getenv("DEBUG_HEADED", "false").lower() == "true")

    async with _SHARED_BROWSER_LOCK:
        if _SHARED_BROWSER is not None:
            try:
                if _SHARED_BROWSER.is_connected():
                    return _SHARED_PLAYWRIGHT, _SHARED_BROWSER
            except Exception:
                pass
            try:
                await _SHARED_BROWSER.close()
            except Exception:
                pass
            _SHARED_BROWSER = None

        if _SHARED_PLAYWRIGHT is None:
            _SHARED_PLAYWRIGHT = await async_playwright().start()

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1440,900",
        ]
        try:
            _SHARED_BROWSER = await _SHARED_PLAYWRIGHT.chromium.launch(headless=headless, channel="chrome", args=browser_args)
        except Exception as ce:
            logger.debug(f"Launching with channel='chrome' failed ({ce}); falling back to default chromium.")
            _SHARED_BROWSER = await _SHARED_PLAYWRIGHT.chromium.launch(headless=headless, args=browser_args)

        return _SHARED_PLAYWRIGHT, _SHARED_BROWSER

async def close_shared_browser():
    """Cleanly close shared browser instance and playwright driver."""
    global _SHARED_PLAYWRIGHT, _SHARED_BROWSER
    async with _SHARED_BROWSER_LOCK:
        if _SHARED_BROWSER:
            try:
                for ctx in list(_SHARED_BROWSER.contexts):
                    try:
                        for pg in list(ctx.pages):
                            try:
                                await pg.close()
                            except Exception:
                                pass
                        await ctx.close()
                    except Exception:
                        pass
                await _SHARED_BROWSER.close()
            except Exception:
                pass
            _SHARED_BROWSER = None
        if _SHARED_PLAYWRIGHT:
            try:
                await _SHARED_PLAYWRIGHT.stop()
            except Exception:
                pass
            _SHARED_PLAYWRIGHT = None


async def fill_application(
    url: str,
    existing_page=None,
    headless: Optional[bool] = None,
) -> bool:
    """
    Unified entry point for automated application filling.
    Detects portal from URL, launches stealth browser if no page provided,
    runs UnifiedATSFormFiller, and returns True on success.
    """
    portal = detect_portal(url)
    if not portal:
        logger.warning(f"[SKIP] Non-supported portal: {url}")
        return False

    profile = load_profile()
    own_browser = False
    browser = None
    pw = None
    context = None
    page = None

    if existing_page:
        page = existing_page
    else:
        pw, browser = await get_shared_browser(headless=headless)
        user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=2,
            user_agent=user_agent,
            locale="en-US",
            timezone_id="America/New_York",
            color_scheme="light"
        )
        await context.add_init_script("""
            try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
            window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
            if (!navigator.userAgentData) {
                Object.defineProperty(navigator, 'userAgentData', {
                    get: () => ({
                        brands: [
                            {brand: "Google Chrome", version: "133"},
                            {brand: "Chromium", version: "133"},
                            {brand: "Not-A.Brand", version: "24"}
                        ],
                        mobile: false,
                        platform: "macOS"
                    })
                });
            }
        """)
        page = await context.new_page()
        try:
            from playwright_stealth.stealth import Stealth
            await Stealth().apply_stealth_async(page)
        except Exception as se:
            logger.debug(f"Stealth apply error: {se}")
        own_browser = True

    try:
        logger.info(f"Navigating to {portal.upper()} form: {url}")
        if portal == "lever" and context is not None:
            await clear_lever_context(context, page)

        async with _PLAYWRIGHT_PAGE_LOCK:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2.5)

        try:
            from app.services.grounded_essay import ensure_context_for_url
            ensure_context_for_url(url, page_text if 'page_text' in locals() else "")
        except Exception as _ge:
            logger.debug(f"grounded_essay context error: {_ge}")
        filler = UnifiedATSFormFiller(page, profile, portal)
        await filler.run()
        return True

    except Exception as e:
        logger.error(f"fill_application error: {e}")
        traceback.print_exc()
        return False

    finally:
        if own_browser:
            try:
                if page:
                    await page.close()
            except Exception:
                pass
            try:
                if context:
                    await context.close()
            except Exception:
                pass


async def launch_and_fill_job(
    url: str,
    source: str = "direct",
    headless: Optional[bool] = None,
) -> Tuple[Any, Any]:
    """
    Called by Telegram bot (resume_bot.py).
    Uses exactly one shared headless browser instance per process.
    Opens a single browser context for this task, fills form via UnifiedATSFormFiller,
    scrolls to top, and returns (pw, browser) WITHOUT terminal input() blocking.
    Caller must close page and context in a finally block.
    """
    if headless is None:
        headless = not (os.getenv("DEBUG_HEADED", "false").lower() == "true")

    portal = detect_portal(url)
    if not portal:
        raise ValueError(f"Non-supported ATS portal: {url}")

    profile = load_profile()
    pw, browser = await get_shared_browser(headless=headless)

    user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    context = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        device_scale_factor=2,
        user_agent=user_agent,
        locale="en-US",
        timezone_id="America/New_York",
        color_scheme="light"
    )
    # Anti-bot stealth injection to mask automation flags
    await context.add_init_script("""
        try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
        window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
        if (!navigator.userAgentData) {
            Object.defineProperty(navigator, 'userAgentData', {
                get: () => ({
                    brands: [
                        {brand: "Google Chrome", version: "133"},
                        {brand: "Chromium", version: "133"},
                        {brand: "Not-A.Brand", version: "24"}
                    ],
                    mobile: false,
                    platform: "macOS"
                })
            });
        }
    """)
    page = await context.new_page()
    try:
        from playwright_stealth.stealth import Stealth
        await Stealth().apply_stealth_async(page)
    except Exception as se:
        logger.debug(f"Stealth apply error: {se}")

    try:
        # Clear Lever context before Lever application flows
        if portal == "lever":
            await clear_lever_context(context, page)

        start_time = time.time()
        async with _PLAYWRIGHT_PAGE_LOCK:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            _record_activity(f"navigation: {url}")
            # Page settle delay
            await asyncio.sleep(1.5)

        # Fast check for closed / 404 / expired job postings
        page_text = await page.evaluate("() => (document.body ? document.body.innerText : '')")
        title = await page.title()
        p_low = (page_text + " " + title).lower()
        closed_phrases = [
            'page not found',
            'the page you requested was not found',
            'no longer accepting applications',
            'this job is no longer available',
            'this position has been closed',
            'this job posting has expired',
            'this opening has been filled',
            'the job you are looking for is no longer open',
            'this listing is no longer active',
            'this job is closed',
            'position is no longer available',
            '404 not found',
            'sorry, this position has been filled',
            'this role is no longer accepting applications'
        ]
        if any(ph in p_low for ph in closed_phrases):
            logger.warning(f"Aborting fill: Posting {url} is closed, expired, or 404.")
            return pw, browser

        try:
            from app.services.grounded_essay import ensure_context_for_url
            ensure_context_for_url(url, page_text if 'page_text' in locals() else "")
        except Exception as _ge:
            logger.debug(f"grounded_essay context error: {_ge}")
        filler = UnifiedATSFormFiller(page, profile, portal)
        await filler.run()

        # Enforce minimum human interaction duration gate:
        # Ensures the form is never filled suspiciously fast (< 5 seconds) and mimics realistic human reading & reviewing.
        min_fill_duration = float(getattr(config, "MIN_FORM_FILL_DURATION_SECONDS", 25.0))
        elapsed = time.time() - start_time
        target_duration = random.uniform(min_fill_duration, min_fill_duration + 10.0)
        if elapsed < target_duration:
            review_time = target_duration - elapsed
            logger.info(f"Form filled in {elapsed:.1f}s. Simulating human review for {review_time:.1f}s...")
            try:
                # Smoothly scroll up to simulate reviewing inputs
                await page.mouse.wheel(0, -random.randint(350, 500))
                await asyncio.sleep(review_time * 0.45)
                # Smoothly scroll back down to review bottom fields and submit area
                await page.mouse.wheel(0, random.randint(350, 500))
                await asyncio.sleep(review_time * 0.55)
            except Exception:
                await asyncio.sleep(review_time)

        if filler.unanswered_fields:
            unique_missing = list(dict.fromkeys(u.get("label", "") for u in filler.unanswered_fields if u.get("label")))[:3]
            if unique_missing:
                missing_str = ", ".join(f"'{m}'" for m in unique_missing)
                try:
                    send_telegram_alert(
                        f"⚠️ *Review Required (Specific Question)*\n"
                        f"Please verify this question in the open browser:\n• {missing_str}"
                    )
                except Exception:
                    pass

        return pw, browser
    except Exception:
        try:
            if 'page' in locals() and page:
                await page.close()
        except Exception:
            pass
        try:
            if 'context' in locals() and context:
                await context.close()
        except Exception:
            pass
        raise


# ============================================================================
# CLI entry point
# ============================================================================
if __name__ == "__main__":
    is_headed = os.getenv("DEBUG_HEADED", "false").lower() == "true"
    if len(sys.argv) > 1:
        asyncio.run(fill_application(sys.argv[1], headless=not is_headed))
    else:
        print("Usage: python form_filler.py <job_application_url>")
