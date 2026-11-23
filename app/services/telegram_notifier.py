"""
AutoApply - Centralized Telegram Alert Helper & Message Hygiene
Enforces causal ordering, exactly-once delivery per (job_id, alert_type),
state-aware suppression (e.g. no pacing message after halt/block/complete),
and consistent alert headers (company, role, portal, job URL, timestamp).
"""
import os
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import httpx
try:
    from config import settings
    if not hasattr(settings, "DATA_DIR"):
        raise AttributeError("settings missing DATA_DIR")
except Exception:
    import importlib.util
    _p = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "config.py"))
    _spec = importlib.util.spec_from_file_location("sid_job_scrip_config", _p)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    settings = _mod.settings

logger = logging.getLogger(__name__)

# In-memory deduplication and state tracking
_SENT_ALERTS = set()
_JOB_STATES: Dict[str, str] = {}


def reset_alert_hygiene():
    """Reset dedupe caches for testing."""
    _SENT_ALERTS.clear()
    _JOB_STATES.clear()


def set_job_state(job_id: str, state: str):
    """Update lifecycle state for a job (active, halted, blocked, completed)."""
    if job_id:
        _JOB_STATES[job_id] = state


def get_job_state(job_id: str) -> str:
    """Retrieve current lifecycle state for a job."""
    return _JOB_STATES.get(job_id, "active")


async def send_telegram_alert(
    job_id: str,
    alert_type: str,
    company: str,
    role: str,
    portal: str,
    job_url: str,
    details: str = "",
    buttons: Optional[List[List[Dict[str, str]]]] = None,
    screenshot_path: Optional[str] = None,
    verified_success: bool = False,
    force: bool = False
) -> bool:
    """
    Central dispatch for all Telegram notifications.
    Guarantees:
    1. Exactly-once delivery per (job_id, alert_type)
    2. State-aware suppression (no pacing when halted/blocked/completed)
    3. No success alerts without verified post-submit confirmation
    4. Uniform schema: company, role, portal, job URL, timestamp
    """
    current_state = get_job_state(job_id)

    # State-aware suppression rule: Never send pacing countdowns if job is halted, blocked, or completed
    if alert_type == "pacing" and current_state in ("halted", "blocked", "completed"):
        logger.info(f"Suppressed pacing alert for job {job_id} in state '{current_state}'")
        return False

    # Verification gate rule: Never claim success unless verification confirmed
    if alert_type == "submitted" and not verified_success:
        logger.warning(f"Suppressed unverified submission success alert for {company}")
        return False

    # Update lifecycle state on terminal or halting alert types
    if alert_type in ("halt_review", "missing_field", "captcha", "security_challenge", "pre_submit_flagged"):
        set_job_state(job_id, "halted")
    elif alert_type in ("blocked", "cap_reached", "capped", "employer_limit", "candidate_limit"):
        set_job_state(job_id, "blocked")
    elif alert_type == "submitted":
        set_job_state(job_id, "completed")

    # NEW NOTIFICATION GATE: SUCCESS RECEIPTS ONLY + SPAM_FLAG_NOTICE.
    # Telegram pushes SUCCESS RECEIPTS ONLY and SPAM_FLAG_NOTICE. Everything else goes silent:
    # no cap-limit notices, no CAPTCHA_SOLVE_REQUEST, no MISSING_INFO_REQUEST.
    # Blocked or incomplete applications resolve quietly in the ledger.
    if getattr(settings, "TELEGRAM_SUCCESS_ONLY", True):
        is_success = (alert_type == "submitted" and verified_success)
        is_spam_flag = (alert_type in ("spam_flag_notice", "spam_flag", "spam"))
        is_no_app = (alert_type in ("no_application_found", "hourly_no_application"))
        if not (is_success or is_spam_flag or is_no_app):
            logger.info(
                f"Telegram alert permanently suppressed: SUCCESS RECEIPTS ONLY gate active "
                f"(alert_type='{alert_type}', verified={verified_success}) on {company}. Recorded in ledger/logs only."
            )
            return False

    # STRICT NOISE ELIMINATION (GUARANTEED NOISE BOUNDARY - LEGACY ALLOWLIST):
    # Only verified successes ("submitted"), portal spam flags ("spam_flag_notice"), cap-limit notices,
    # and the Part 2 hourly scan "no application found" single short line reach Telegram.
    # Human-in-the-loop blocks (captcha_solve_request, missing_info_request) also reach Telegram —
    # exactly ONE message per block per application, no repeats, silence on timeout.
    # Diagnostics failures ("system_health") trigger a single health alert.
    # ALL security challenges, skips, and validation errors appear in the end-of-run digest ONLY.
    if alert_type not in (
        "submitted",
        "no_application_found",
        "hourly_no_application",
        "spam_flag_notice",
        "spam_flag",
        "spam",
        "cap_reached", "capped", "employer_limit", "candidate_limit",
        "captcha_solve_request",   # HITL A: CAPTCHA freeze — exactly one message per block
        "missing_info_request",    # HITL B: missing required field — exactly one message per block
        "system_health",
    ):
        logger.info(f"Telegram alert permanently suppressed for non-permitted alert_type '{alert_type}' on {company}. Recorded in logs/digest only.")
        return False

    # Exactly-once deduplication key
    dedupe_key = f"{job_id}:{alert_type}"
    if not force and dedupe_key in _SENT_ALERTS:
        logger.debug(f"Suppressed duplicate alert: {dedupe_key}")
        return False

    _SENT_ALERTS.add(dedupe_key)

    # Header and emoji mapping - strictly allowed types
    icons = {
        "cap_reached": "🛑",
        "capped": "🛑",
        "employer_limit": "🛑",
        "candidate_limit": "🛑",
        "submitted": "✅",
        "spam_flag_notice": "🚨",
        "spam_flag": "🚨",
        "spam": "🚨",
        "system_health": "🚨",
        "captcha_solve_request": "🧩",
        "missing_info_request": "❓",
    }
    icon = icons.get(alert_type, "📢")
    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %I:%M %p UTC")

    # Rename employer-level candidate-limit notice from 'Per-Company Cap Reached' to 'Employer Candidate Limit'
    is_employer_lock = (
        alert_type in ("employer_limit", "candidate_limit")
        or "employer candidate limit" in details.lower()
        or "candidate limit" in details.lower()
        or "candidate application limit" in details.lower()
    )
    if is_employer_lock:
        header_title = "Employer Candidate Limit"
    elif alert_type in ("spam_flag_notice", "spam_flag", "spam"):
        header_title = "Action Required: Portal Spam Flag"
    elif alert_type in ("cap_reached", "capped"):
        header_title = "Per-Company Cap Reached"
    elif alert_type == "captcha_solve_request":
        header_title = "CAPTCHA Solve Required"
    elif alert_type == "missing_info_request":
        header_title = "Missing Information Required"
    else:
        header_title = alert_type.replace('_', ' ').title()

    # Construct clean, consistent markdown message
    lines = [
        f"{icon} *{header_title}*",
        f"🏢 *Company:* {company}",
        f"💼 *Role:* {role or 'Software Engineer'}",
        f"🌐 *Portal:* {portal or 'Direct'}",
        f"🔗 [Direct Link]({job_url})",
        f"🕒 *Time:* {time_str}",
    ]

    if details:
        lines.append("")
        lines.append(details.strip())

    text = "\n".join(lines)

    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.info(f"Telegram not configured. Alert logged: {dedupe_key}")
        return True

    base_url = f"https://api.telegram.org/bot{token}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }

            if buttons:
                payload["reply_markup"] = {"inline_keyboard": buttons}

            # If screenshot provided and exists, send photo with caption
            if screenshot_path and os.path.exists(screenshot_path):
                try:
                    with open(screenshot_path, "rb") as f:
                        files = {"photo": f}
                        data = {
                            "chat_id": chat_id,
                            "caption": text[:1024],
                            "parse_mode": "Markdown"
                        }
                        if buttons:
                            data["reply_markup"] = payload["reply_markup"]
                        resp = await client.post(f"{base_url}/sendPhoto", data=data, files=files)
                        if resp.status_code == 200:
                            return True
                        # If Markdown parsing failed (status 400), retry with plain caption
                        f.seek(0)
                        plain_data = dict(data)
                        plain_data.pop("parse_mode", None)
                        resp = await client.post(f"{base_url}/sendPhoto", data=plain_data, files=files)
                        if resp.status_code == 200:
                            return True

                        # Bounded backoff retry for photo send (standing rule: never a success without snapshot)
                        for backoff in [1.0, 2.0]:
                            await asyncio.sleep(backoff)
                            f.seek(0)
                            resp = await client.post(f"{base_url}/sendPhoto", data=plain_data, files=files)
                            if resp.status_code == 200:
                                return True
                except Exception as pe:
                    logger.debug(f"Failed sending photo alert: {pe}")

            # ATOMICITY ENFORCEMENT: Never a success notification without its snapshot!
            # If sendPhoto fails after the plaintext retry and bounded backoff:
            # do NOT fall back to text-only — suppress the Telegram ping entirely.
            if alert_type == "submitted":
                logger.warning(
                    f"Atomic success receipt: photo send failed across all retries for {company}. "
                    "Suppressing text-only Telegram ping (ledger marked photo_pending)."
                )
                return False

            resp = await client.post(f"{base_url}/sendMessage", json=payload)
            return resp.status_code == 200

    except Exception as e:
        logger.error(f"Error sending Telegram alert ({dedupe_key}): {e}")
        return False


async def send_hourly_no_application_telegram() -> bool:
    """
    Part 2 Hourly Scan Cycle Telegram Update:
    If nothing was submitted that hour, send one Telegram update:
    a single short line stating no application was found to submit this hour.
    No digest, no extra detail, one message only.
    """
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.info("Telegram not configured. Hourly no-application notification logged locally.")
        return True

    text = "No application was found to submit this hour."
    base_url = f"https://api.telegram.org/bot{token}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{base_url}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                }
            )
            return resp.status_code == 200
    except Exception as e:
        logger.error(f"Error sending hourly no-application Telegram line: {e}")
        return False

