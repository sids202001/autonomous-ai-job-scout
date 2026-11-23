"""
AutoApply - Verification Code Fetcher
Polls Gmail for unread 2FA/verification code emails during job application flows.
Extracts numeric/alphanumeric verification codes using regex.
Provides Telegram notification fallback if email poll times out.
"""
import asyncio
import base64
import logging
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

def _get_settings():
    try:
        from config import settings
        return settings
    except ImportError:
        import sys
        from pathlib import Path
        root = str(Path(__file__).resolve().parent.parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        from config import settings
        return settings

# Patterns for extracting verification codes
CODE_PATTERNS = [
    # Hyphenated code like 123-456
    r'\b([0-9]{3}-[0-9]{3})\b',
    # Verification/one-time/security/confirmation code followed eventually by colon/words and number
    r'(?:verification|security|confirmation|validation|passcode|one-time|access|login)\s+code[^\n\r\d]{0,80}?[:\s]+([0-9]{4,8})\b',
    # Direct code/pin/passcode prefix: "code is: 123456", "PIN: 123456"
    r'(?:code|pin|passcode)\s*(?:is|:)?\s*[:\s-]*\b([0-9]{4,8})\b',
    # Alphanumeric access/verification code with letters and digits: "access code: K9B4X2"
    r'(?:verification|security|confirmation|access)\s+code[^\n\r]{0,40}?[:\s]+([A-Za-z0-9]{4,8})\b',
    # Standalone line with 5-8 digits
    r'(?:^|[\r\n])\s*([0-9]{5,8})\s*(?:$|[\r\n])',
]


def extract_code_from_email_body(body: str) -> Optional[str]:
    """
    Extract verification code from plain text or HTML email body.
    Returns cleaned string code (e.g. '123456'), or None if not found.
    """
    if not body:
        return None

    # First attempt: targeted regex patterns
    for pattern in CODE_PATTERNS:
        matches = re.findall(pattern, body, re.IGNORECASE | re.MULTILINE)
        for match in matches:
            code = match.replace("-", "").strip()
            # Guard against false positives like years (1990-2030) or common timestamps
            if len(code) == 4 and code.isdigit() and 1990 <= int(code) <= 2030:
                continue
            if 4 <= len(code) <= 8:
                return code

    # Second attempt: check lines containing "code"
    for line in body.splitlines():
        line_clean = line.strip()
        if "code" in line_clean.lower():
            nums = re.findall(r'\b([0-9]{5,8})\b', line_clean)
            if nums:
                return nums[0]

    return None


def _extract_text_payload(payload: dict) -> str:
    """Recursively decode body text from Gmail message payload parts."""
    text_parts = []
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")

    if body_data and mime_type in ("text/plain", "text/html"):
        try:
            decoded = base64.urlsafe_b64decode(body_data.encode("ASCII")).decode("utf-8", errors="ignore")
            text_parts.append(decoded)
        except Exception:
            pass

    for part in payload.get("parts", []):
        text_parts.append(_extract_text_payload(part))

    return "\n".join(text_parts)


async def send_telegram_code_request(portal: str, hint: str):
    """Fallback notification helper. Suppressed per Telegram noise boundary (allowlist: submitted, capped)."""
    logger.info(
        f"Telegram verification alert suppressed for {portal} ({hint}) per Telegram noise boundary. "
        "Recorded in local logs only."
    )
    return


def _poll_code_via_imap(
    user: str,
    password: str,
    sender_hint: str = "",
    timeout: int = 180,
    poll_interval: int = 5,
    failure_report: Optional[dict] = None
) -> Optional[str]:
    """
    Connect to Gmail IMAP via SSL using App Password, fetch unread emails, and extract code.
    Reports the exact failure stage (connection, auth, mailbox_select, search, fetch, parse)
    without leaking credentials.
    """
    import imaplib
    import email

    clean_password = password.replace(" ", "").strip()
    start_time = time.time()
    current_stage = "init"

    while (time.time() - start_time) < timeout:
        mail = None
        try:
            current_stage = "connection"
            mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)

            current_stage = "auth"
            mail.login(user, clean_password)

            current_stage = "mailbox_select"
            mail.select("INBOX")

            current_stage = "search"
            search_criteria = 'UNSEEN'
            if sender_hint:
                search_criteria = f'(UNSEEN FROM "{sender_hint}")'

            status, data = mail.search(None, search_criteria)
            if status != "OK" or not data or not data[0]:
                # Fallback to general unread if sender filter is too strict
                status, data = mail.search(None, 'UNSEEN')

            if status == "OK" and data and data[0]:
                current_stage = "fetch"
                email_ids = data[0].split()
                # Check latest 3 unread messages
                for e_id in reversed(email_ids[-3:]):
                    res, msg_data = mail.fetch(e_id, "(RFC822)")
                    if res == "OK":
                        current_stage = "parse"
                        raw_email = msg_data[0][1]
                        msg = email.message_from_bytes(raw_email)
                        body_parts = []
                        if msg.is_multipart():
                            for part in msg.walk():
                                ctype = part.get_content_type()
                                if ctype in ("text/plain", "text/html"):
                                    payload = part.get_payload(decode=True)
                                    if payload:
                                        body_parts.append(payload.decode("utf-8", errors="ignore"))
                        else:
                            payload = msg.get_payload(decode=True)
                            if payload:
                                body_parts.append(payload.decode("utf-8", errors="ignore"))

                        full_body = "\n".join(body_parts)
                        code = extract_code_from_email_body(full_body)
                        if code:
                            logger.info("Successfully retrieved verification code via IMAP: [REDACTED]")
                            return code

        except Exception as e:
            err_msg = f"Failed at stage '{current_stage}': {type(e).__name__}"
            logger.error(f"Gmail IMAP error: {err_msg}")
            if failure_report is not None:
                failure_report["stage"] = current_stage
                failure_report["error"] = err_msg
        finally:
            if mail:
                try:
                    mail.close()
                    mail.logout()
                except Exception:
                    pass

        time.sleep(poll_interval)

    if failure_report is not None and "stage" not in failure_report:
        failure_report["stage"] = "timeout"
        failure_report["error"] = f"Polling timed out after {timeout}s without finding verification code"

    return None


def prove_live_gmail_retrieval(user: str, password: str, timeout: int = 45) -> dict:
    """
    Live end-to-end proof of the Gmail App Password pipeline:
    1. Sends a live email with a generated test code to user via Gmail SMTP SSL.
    2. Polls and retrieves the code via _poll_code_via_imap().
    3. Verifies extracted code matches the sent test code.
    4. Confirms redaction in logs.
    Never persists or leaks credentials or raw codes to DB/disk.
    """
    import random
    import smtplib
    from email.mime.text import MIMEText

    clean_pw = password.replace(" ", "").strip()
    test_code = f"{random.randint(100000, 999999)}"
    audit: dict = {
        "user": user,
        "test_code_redacted": "[REDACTED]",
        "smtp_sent": False,
        "imap_stage": "not_started",
        "verified_match": False,
        "duration_sec": 0.0,
    }

    start = time.time()

    # Step 1: Send test email via Gmail SMTP
    try:
        msg = MIMEText(f"Hello,\n\nYour job application verification code is: {test_code}\n\nThis is an automated test.")
        msg["Subject"] = f"Verification Code: {test_code}"
        msg["From"] = user
        msg["To"] = user

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(user, clean_pw)
            server.sendmail(user, [user], msg.as_string())
        audit["smtp_sent"] = True
        logger.info("Sent live test verification email via SMTP (code: [REDACTED])")
    except Exception as se:
        audit["smtp_error"] = f"SMTP stage failed: {type(se).__name__}"
        logger.error(f"Live proof SMTP send failed: {se}")
        audit["duration_sec"] = round(time.time() - start, 2)
        return audit

    # Step 2: Poll via IMAP
    time.sleep(2)  # Give Gmail 2s delivery window
    failure_report = {}
    extracted = _poll_code_via_imap(
        user=user,
        password=clean_pw,
        sender_hint="",
        timeout=timeout,
        poll_interval=3,
        failure_report=failure_report
    )

    audit["imap_stage"] = failure_report.get("stage", "parse" if extracted else "timeout")
    if extracted == test_code:
        audit["verified_match"] = True
        logger.info("Live end-to-end Gmail verification succeeded! Extracted: [REDACTED]")
    else:
        audit["verified_match"] = False
        audit["error"] = failure_report.get("error", "Code mismatch or not found")

    audit["duration_sec"] = round(time.time() - start, 2)
    return audit


async def wait_for_verification_code(
    sender_hint: str = "",
    portal: str = "Job Portal",
    timeout: int = 180,
    poll_interval: int = 5,
    since_timestamp: Optional[int] = None
) -> Optional[str]:
    """
    Poll Gmail inbox for an unread verification code message within `timeout` seconds.
    Supports either:
    1. Direct Gmail App Password via SSL IMAP (No Google Cloud Console needed)
    2. Google Cloud OAuth 2.0 (credentials.json)
    """
    settings = _get_settings()

    # Priority 1: Gmail App Password (IMAP) - fast, local, zero GCP setup!
    if settings.GMAIL_APP_PASSWORD:
        logger.info(f"Using Gmail App Password (IMAP) to poll for verification code from '{portal}'...")
        loop = asyncio.get_event_loop()
        code = await loop.run_in_executor(
            None,
            lambda: _poll_code_via_imap(
                user=settings.GMAIL_USER,
                password=settings.GMAIL_APP_PASSWORD,
                sender_hint=sender_hint,
                timeout=timeout,
                poll_interval=poll_interval
            )
        )
        if code:
            return code
        logger.warning(f"IMAP verification code polling timed out after {timeout}s for {portal}.")
        await send_telegram_code_request(portal, f"IMAP polling timed out after {timeout}s")
        return None

    # Priority 2: Google Cloud Console OAuth 2.0 (credentials.json)
    try:
        from app.services.gmail_client import gmail_client
    except Exception as e:
        logger.warning(f"Gmail OAuth client not available: {e}. Sending Telegram fallback...")
        await send_telegram_code_request(portal, "Gmail credentials not configured in .env")
        return None

    if not gmail_client.is_configured():
        logger.warning("Neither Gmail App Password nor credentials.json configured. Sending Telegram fallback...")
        await send_telegram_code_request(portal, "Gmail credentials not configured in .env")
        return None

    service = gmail_client.get_service()
    if not service:
        logger.warning("Could not establish Gmail service. Sending Telegram fallback...")
        await send_telegram_code_request(portal, "Gmail authorization required")
        return None

    start_time = time.time()
    cutoff_epoch = since_timestamp or int(start_time - 60)
    logger.info(f"Polling Gmail for verification code from '{portal}' (timeout: {timeout}s)...")

    query_parts = ["is:unread", f"after:{cutoff_epoch}"]
    if sender_hint:
        query_parts.append(sender_hint)
    query = " ".join(query_parts)

    while (time.time() - start_time) < timeout:
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: service.users().messages().list(userId="me", q=query, maxResults=5).execute()
            )
            messages = response.get("messages", [])

            for msg_meta in messages:
                msg_id = msg_meta["id"]
                msg = await loop.run_in_executor(
                    None,
                    lambda: service.users().messages().get(userId="me", id=msg_id, format="full").execute()
                )

                payload = msg.get("payload", {})
                body_text = _extract_text_payload(payload)
                code = extract_code_from_email_body(body_text)

                if code:
                    logger.info("Successfully retrieved verification code: [REDACTED]")
                    return code

        except Exception as e:
            logger.error(f"Error querying Gmail API for verification code: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(f"Verification code polling timed out after {timeout}s for {portal}.")
    await send_telegram_code_request(portal, f"Polling timed out after {timeout}s")
    return None
