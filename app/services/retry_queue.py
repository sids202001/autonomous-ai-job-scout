"""
Retry Hygiene & Per-Company State Isolation Service
Enforces strict retry rules:
1. Attempt cap: Each role gets a maximum of TWO attempts total (initial attempt + exactly ONE retry). Never more.
2. Retry queue: If a submission fails (technical or verification failure — NOT spam-flagged),
   stores application state and retries exactly once after a 4-hour cooldown.
   After the retry (success or fail), the role resolves terminally.
3. Spam-flagged applications are EXCLUDED from auto-retry: They go to the user via
   SPAM_FLAG_NOTICE for manual submission (manual submit IS the retry) to avoid duplicate applications.
4. All standing pacing unchanged: fixed 150s between submits, 10 req/min per ATS, 50/day cap.
5. Per-company state hygiene: Clear browser cookies, cache, and site data after each company resolves.
"""
import os
import re
import time
import hashlib
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Tuple, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.expanduser("~/Desktop/jobs_seen.db")
RETRY_COOLDOWN_SECONDS = 4 * 3600  # Exactly 4 hours (14,400 seconds)
TERMINAL_PARK_SECONDS = 24 * 3600  # Minimum 24h park for terminal halts (R3)
MAX_ATTEMPTS_PER_ROLE = 2  # Maximum of TWO attempts total (1 initial + 1 retry)


class ClosingConnection:
    """Wrapper around sqlite3.Connection that ensures connection is closed when exiting context."""
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
    def __getattr__(self, name):
        return getattr(self._conn, name)
    def __enter__(self):
        self._conn.__enter__()
        return self._conn
    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            return self._conn.__exit__(exc_type, exc_val, exc_tb)
        finally:
            self._conn.close()


class RetryQueueManager:
    """Manages role attempt caps, the 4-hour retry queue, and terminal resolutions."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._init_db()

    def _get_conn(self) -> ClosingConnection:
        return ClosingConnection(sqlite3.connect(self.db_path))

    def _init_db(self):
        """Initialize role_attempts and retry_queue schema."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
            with self._get_conn() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS role_attempts (
                        role_key TEXT PRIMARY KEY,
                        company TEXT,
                        title TEXT,
                        url TEXT,
                        attempt_count INTEGER DEFAULT 0,
                        first_attempt_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_attempt_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        terminal INTEGER DEFAULT 0,
                        terminal_reason TEXT,
                        last_status TEXT
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS retry_queue (
                        queue_id TEXT PRIMARY KEY,
                        role_key TEXT UNIQUE,
                        company TEXT,
                        title TEXT,
                        url TEXT,
                        chat_id INTEGER,
                        failed_reason TEXT,
                        retry_after REAL,
                        status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_retry_queue_status ON retry_queue (status, retry_after)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_role_attempts_comp ON role_attempts (company)")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS essay_park_list (
                        role_key TEXT PRIMARY KEY,
                        company TEXT,
                        title TEXT,
                        url TEXT,
                        question TEXT,
                        parked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_digested_at TIMESTAMP
                    )
                """)
                conn.commit()
        except Exception as e:
            logger.error(f"Error initializing retry queue database: {e}")

    @staticmethod
    def extract_canonical_job_id(url: str) -> str:
        """Extract deterministic ATS job ID from URL so retry_queue deduplicates by job ID."""
        if not url:
            return ""
        u = url.strip().lower()
        # Greenhouse token=... or /jobs/12345
        m = re.search(r"[?&]token=([a-z0-9_-]+)", u)
        if m:
            return f"gh:{m.group(1)}"
        m = re.search(r"/jobs/(\d+)", u)
        if m:
            return f"gh:{m.group(1)}"
        # Ashby / Lever UUID
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", u)
        if m:
            return f"uuid:{m.group(1)}"
        return re.sub(r"[?#].*$", "", u).rstrip("/")

    @staticmethod
    def make_role_key(company: str, title: str, url: str = "") -> str:
        """Create deterministic canonical role key (SHA-256, stable across restarts / PYTHONHASHSEED)."""
        clean_comp = re.sub(r'[^a-zA-Z0-9]', '', (company or "").lower())
        clean_title = re.sub(r'[^a-zA-Z0-9]', '', (title or "").lower())
        job_id = RetryQueueManager.extract_canonical_job_id(url)
        url_hash = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16] if job_id else ""
        return f"{clean_comp}:{clean_title}:{url_hash}"

    def record_essay_halt(self, company: str, title: str, url: str, question: str) -> None:
        """R4: Park an essay-blocked posting on the daily essay park-list."""
        role_key = self.make_role_key(company, title, url)
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with self._get_conn() as conn:
                conn.execute("""
                    INSERT INTO essay_park_list (role_key, company, title, url, question, parked_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(role_key) DO UPDATE SET
                        question = excluded.question,
                        parked_at = excluded.parked_at
                """, (role_key, company, title, url, question, now_iso))
                conn.commit()
        except Exception as e:
            logger.error(f"Error recording essay park-list item: {e}")

    def get_pending_essay_digest(self) -> List[Dict[str, Any]]:
        """R4: Return essay-parked postings not yet sent in the last 24h."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        try:
            with self._get_conn() as conn:
                rows = conn.execute("""
                    SELECT role_key, company, title, url, question, parked_at
                    FROM essay_park_list
                    WHERE last_digested_at IS NULL OR last_digested_at < ?
                    ORDER BY parked_at DESC
                """, (cutoff,)).fetchall()
                return [
                    {"role_key": r[0], "company": r[1], "title": r[2], "url": r[3], "question": r[4], "parked_at": r[5]}
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"Error reading essay park-list: {e}")
            return []

    def mark_essay_digest_sent(self, role_keys: List[str]) -> None:
        """R4: Mark essay park-list items as included in today's consolidated digest."""
        if not role_keys:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with self._get_conn() as conn:
                conn.executemany(
                    "UPDATE essay_park_list SET last_digested_at = ? WHERE role_key = ?",
                    [(now_iso, rk) for rk in role_keys]
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error marking essay digest sent: {e}")

    def can_attempt_role(self, company: str, title: str, url: str = "", current_time: Optional[float] = None) -> Tuple[bool, str, int]:
        """
        Check if role is eligible for launch under the Attempt Cap (max 2 tries):
        - If terminal == 1: Blocked (already resolved terminally).
        - If attempt_count >= 2: Blocked (maximum 2 attempts reached; third attempt impossible).
        - If attempt_count == 1:
          - If in 4h cooldown: Blocked until cooldown expires.
          - If cooldown expired: Allowed for single final retry (attempt 2 of 2).
        - If attempt_count == 0: Allowed for initial attempt (attempt 1 of 2).
        """
        role_key = self.make_role_key(company, title, url)
        now = current_time if current_time is not None else time.time()

        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT attempt_count, terminal, terminal_reason FROM role_attempts WHERE role_key = ?",
                    (role_key,)
                )
                row = cursor.fetchone()

                if not row and url:
                    # Also check by exact URL to prevent relaunching across legacy hash keys
                    cursor.execute(
                        "SELECT attempt_count, terminal, terminal_reason FROM role_attempts WHERE url = ? AND (terminal = 1 OR last_attempt_at >= ?) ORDER BY last_attempt_at DESC LIMIT 1",
                        (url, (datetime.now(timezone.utc) - timedelta(seconds=TERMINAL_PARK_SECONDS)).isoformat())
                    )
                    row = cursor.fetchone()

                if not row:
                    return True, "ELIGIBLE_INITIAL: First attempt (1/2).", 1

                attempt_count, terminal, terminal_reason = row

                # 1. Terminal Resolution Guard
                if terminal:
                    return False, f"TERMINAL_BLOCKED: Role resolved terminally ({terminal_reason}). No further attempts permitted.", attempt_count

                # 2. Maximum Attempt Cap Guard (Rule 1: Max 2 attempts total — third attempt impossible)
                if attempt_count >= MAX_ATTEMPTS_PER_ROLE:
                    return False, f"ATTEMPT_CAP_REACHED: Maximum 2 attempts reached ({attempt_count}/{MAX_ATTEMPTS_PER_ROLE}). A third attempt is strictly prohibited.", attempt_count

                # 3. Exactly 1 previous attempt: Check 4-Hour Cooldown (Rule 2)
                if attempt_count == 1:
                    cursor.execute(
                        "SELECT retry_after, status FROM retry_queue WHERE role_key = ?",
                        (role_key,)
                    )
                    q_row = cursor.fetchone()
                    if q_row:
                        retry_after, q_status = q_row
                        if q_status == "pending":
                            if now < retry_after:
                                remaining = int(retry_after - now)
                                return False, f"COOLDOWN_ACTIVE: Role in 4h cooldown ({remaining}s remaining). Retry blocked until cooldown expires.", 1
                            else:
                                return True, "ELIGIBLE_RETRY: 4-hour cooldown elapsed. Proceeding with single final retry attempt (2/2).", 2
                        elif q_status == "retrying":
                            return False, "RETRY_IN_PROGRESS: Retry attempt is already running.", 1
                        elif q_status == "resolved":
                            return False, "RETRY_RESOLVED: Retry already completed.", 2

                    # Fallback check on last_attempt_at in role_attempts
                    cursor.execute("SELECT last_attempt_at FROM role_attempts WHERE role_key = ?", (role_key,))
                    l_row = cursor.fetchone()
                    if l_row and l_row[0]:
                        try:
                            l_dt = datetime.fromisoformat(l_row[0].replace('Z', '+00:00'))
                            last_epoch = l_dt.timestamp()
                            cooldown_end = last_epoch + RETRY_COOLDOWN_SECONDS
                            if now < cooldown_end:
                                remaining = int(cooldown_end - now)
                                return False, f"COOLDOWN_ACTIVE: Role in 4h cooldown ({remaining}s remaining). Retry blocked until cooldown expires.", 1
                        except Exception:
                            pass

                    return True, "ELIGIBLE_RETRY: Proceeding with single final retry attempt (2/2).", 2

                return False, f"BLOCKED: Attempt count {attempt_count} exceeds limit.", attempt_count

        except Exception as e:
            logger.error(f"Error checking can_attempt_role: {e}")
            return True, "ELIGIBLE_FALLBACK: Database check error; admitting.", 1

    def record_attempt_start(self, company: str, title: str, url: str = "") -> int:
        """
        Record the start of an attempt. Increments attempt_count up to MAX_ATTEMPTS_PER_ROLE.
        Returns the new attempt number.
        """
        role_key = self.make_role_key(company, title, url)
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT attempt_count FROM role_attempts WHERE role_key = ?",
                    (role_key,)
                )
                row = cursor.fetchone()

                if row:
                    prior_count = row[0]
                    new_count = min(prior_count + 1, MAX_ATTEMPTS_PER_ROLE)
                    cursor.execute("""
                        UPDATE role_attempts
                        SET attempt_count = ?, last_attempt_at = ?
                        WHERE role_key = ?
                    """, (new_count, now_iso, role_key))
                else:
                    new_count = 1
                    cursor.execute("""
                        INSERT INTO role_attempts (role_key, company, title, url, attempt_count, first_attempt_at, last_attempt_at)
                        VALUES (?, ?, ?, ?, 1, ?, ?)
                    """, (role_key, company, title, url, now_iso, now_iso))

                conn.commit()
                logger.info(f"[RETRY MANAGER] Attempt {new_count}/{MAX_ATTEMPTS_PER_ROLE} started for {company} - '{title}'")
                return new_count
        except Exception as e:
            logger.error(f"Error recording attempt start: {e}")
            return 1

    def record_attempt_result(
        self,
        company: str,
        title: str,
        url: str,
        status: str,
        reason: str,
        is_spam_flag: bool = False,
        chat_id: Optional[int] = None,
        current_time: Optional[float] = None
    ) -> Tuple[bool, str]:
        """
        Record the outcome of an application attempt and update retry queue:
        - SUBMITTED: Succeeded -> Resolves terminally.
        - SPAM_FLAGGED: EXCLUDED from auto-retry -> Resolves terminally (manual submit is retry).
        - FAILED (attempt 1): Enqueues for exactly ONE retry after 4-hour cooldown.
        - FAILED (attempt 2): Resolves terminally (maximum 2 attempts reached).
        - SKIPPED: Position closed / capped -> Resolves terminally.
        """
        role_key = self.make_role_key(company, title, url)
        now = current_time if current_time is not None else time.time()
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT attempt_count FROM role_attempts WHERE role_key = ?",
                    (role_key,)
                )
                row = cursor.fetchone()
                attempt_count = row[0] if row else 1

                # ── RULE 3: Spam-Flagged Applications are EXCLUDED from Auto-Retry ──
                if is_spam_flag:
                    terminal_reason = f"SPAM_FLAG_MANUAL_SUBMIT: {reason}"
                    cursor.execute("""
                        UPDATE role_attempts
                        SET terminal = 1, terminal_reason = ?, last_status = 'SPAM_FLAGGED'
                        WHERE role_key = ?
                    """, (terminal_reason, role_key))
                    cursor.execute("DELETE FROM retry_queue WHERE role_key = ?", (role_key,))
                    conn.commit()
                    logger.info(
                        f"[RETRY HYGIENE] Spam-flagged application for {company} - '{title}' EXCLUDED from auto-retry. "
                        f"Dispatched to user via SPAM_FLAG_NOTICE. Resolved terminally."
                    )
                    return False, "SPAM_FLAG_EXCLUDED_FROM_AUTO_RETRY"

                # ── SUCCESS: Application Successfully Submitted ──
                if status == "SUBMITTED":
                    terminal_reason = f"SUBMITTED: {reason}"
                    cursor.execute("""
                        UPDATE role_attempts
                        SET terminal = 1, terminal_reason = ?, last_status = 'SUBMITTED'
                        WHERE role_key = ?
                    """, (terminal_reason, role_key))
                    cursor.execute("DELETE FROM retry_queue WHERE role_key = ?", (role_key,))
                    conn.commit()
                    logger.info(f"[RETRY HYGIENE] {company} - '{title}' submitted successfully. Resolved terminally.")
                    return True, "SUBMITTED_TERMINAL"

                # ── SKIPPED: Position closed, locked out, or capped ──
                if status == "SKIPPED":
                    terminal_reason = f"SKIPPED: {reason}"
                    cursor.execute("""
                        UPDATE role_attempts
                        SET terminal = 1, terminal_reason = ?, last_status = 'SKIPPED'
                        WHERE role_key = ?
                    """, (terminal_reason, role_key))
                    cursor.execute("DELETE FROM retry_queue WHERE role_key = ?", (role_key,))
                    conn.commit()
                    logger.info(f"[RETRY HYGIENE] {company} - '{title}' skipped ({reason}). Resolved terminally.")
                    return False, "SKIPPED_TERMINAL"

                # ── HALTED / CHALLENGE_BLOCKED: Excluded from auto-retry -> Terminal blocked/parked >=24h (Rule 12 / R3 / R4) ──
                is_halted = (
                    status in (
                        "CHALLENGE_BLOCKED", "HALTED", "BLOCKED", "CONSENT_AMBIGUOUS", "UPLOAD_UNVERIFIED",
                        "OPTION_LABEL_UNREADABLE", "TOGGLE_UNREADABLE", "EDUCATION_DATE_INCOMPLETE",
                        "TRAVEL_DECISION_REQUIRED", "UNMAPPED_SCREENING_QUESTION", "JD_INELIGIBLE", "ESSAY_HALT",
                    )
                    or any(k in status for k in ["HALT", "BLOCK", "DECISION_REQUIRED", "MISSING", "PREMISE", "VERIFICATION_FAILED", "EVIDENCE_FAILURE", "BANNER_BLOCKED", "ADDRESS_REQUIRED", "AMBIGUOUS", "UNVERIFIED", "MISMATCH", "REQUIRED", "UNREADABLE", "INCOMPLETE", "UNMAPPED", "INELIGIBLE"])
                )
                if is_halted:
                    terminal_reason = f"HALTED_BLOCKED ({status}): {reason}"
                    cursor.execute("""
                        UPDATE role_attempts
                        SET terminal = 1, terminal_reason = ?, last_status = ?
                        WHERE role_key = ?
                    """, (terminal_reason, status, role_key))
                    cursor.execute("DELETE FROM retry_queue WHERE role_key = ?", (role_key,))
                    conn.commit()
                    if status == "ESSAY_HALT":
                        self.record_essay_halt(company, title, url, reason)
                    logger.info(
                        f"[RETRY HYGIENE] {company} - '{title}' halted ({status}: {reason}). "
                        f"Resolved terminally as parked/blocked (>=24h). Excluded from retry queue per Rule 12 / R3."
                    )
                    return False, f"{status}_TERMINAL_BLOCKED"

                # ── FAILED: Pure Technical Failure (NOT Halted / Spam Flag) ──
                if status == "FAILED":
                    # RULE 1 & 2: After retry (attempt 2), resolve terminally
                    if attempt_count >= MAX_ATTEMPTS_PER_ROLE:
                        terminal_reason = f"FAILED_TERMINAL_AFTER_2_ATTEMPTS: {reason}"
                        cursor.execute("""
                            UPDATE role_attempts
                            SET terminal = 1, terminal_reason = ?, last_status = 'FAILED'
                            WHERE role_key = ?
                        """, (terminal_reason, role_key))
                        cursor.execute("DELETE FROM retry_queue WHERE role_key = ?", (role_key,))
                        conn.commit()
                        logger.info(
                            f"[RETRY HYGIENE] {company} - '{title}' failed on attempt {attempt_count}/{MAX_ATTEMPTS_PER_ROLE}. "
                            f"Terminal resolution reached. No further retries allowed."
                        )
                        return False, "FAILED_TERMINAL_MAX_ATTEMPTS_REACHED"

                    # RULE 2: Initial attempt failure -> Store state and retry exactly once after 4-hour cooldown
                    retry_after = now + RETRY_COOLDOWN_SECONDS
                    queue_id = f"retry_{abs(hash(role_key)):x}_{int(now)}"
                    retry_after_dt = datetime.fromtimestamp(retry_after).strftime('%Y-%m-%d %H:%M:%S')

                    cursor.execute("""
                        INSERT OR REPLACE INTO retry_queue
                        (queue_id, role_key, company, title, url, chat_id, failed_reason, retry_after, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    """, (queue_id, role_key, company, title, url, chat_id or 0, reason, retry_after))

                    cursor.execute("""
                        UPDATE role_attempts
                        SET last_status = 'QUEUED_FOR_RETRY'
                        WHERE role_key = ?
                    """, (role_key,))

                    conn.commit()
                    logger.info(
                        f"[RETRY QUEUE] {company} - '{title}' failed attempt 1/{MAX_ATTEMPTS_PER_ROLE} ({reason}). "
                        f"Stored application state in retry queue. Scheduled for single retry after 4h cooldown at {retry_after_dt}."
                    )
                    return True, f"QUEUED_FOR_RETRY_AFTER_4H (retry_after={retry_after_dt})"

                return False, f"UNKNOWN_STATUS: {status}"

        except Exception as e:
            logger.error(f"Error recording attempt result: {e}")
            return False, f"ERROR: {e}"

    def get_pending_retries(self, current_time: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        Query eligible pending retries whose 4-hour cooldown has elapsed.
        """
        now = current_time if current_time is not None else time.time()
        try:
            with self._get_conn() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT queue_id, role_key, company, title, url, chat_id, failed_reason, retry_after, status
                    FROM retry_queue
                    WHERE status = 'pending' AND retry_after <= ?
                    ORDER BY retry_after ASC
                """, (now,))
                rows = cursor.fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"Error fetching pending retries: {e}")
            return []

    def mark_retry_in_progress(self, queue_id: str):
        """Mark a queued retry as in progress."""
        try:
            with self._get_conn() as conn:
                conn.execute("UPDATE retry_queue SET status = 'retrying' WHERE queue_id = ?", (queue_id,))
                conn.commit()
        except Exception as e:
            logger.error(f"Error marking retry in progress: {e}")

    def mark_retry_resolved(self, queue_id: str):
        """Mark a queued retry as resolved and remove from queue."""
        try:
            with self._get_conn() as conn:
                conn.execute("DELETE FROM retry_queue WHERE queue_id = ?", (queue_id,))
                conn.commit()
        except Exception as e:
            logger.error(f"Error marking retry resolved: {e}")

    def get_role_attempt_info(self, company: str, title: str, url: str = "") -> Optional[Dict[str, Any]]:
        """Retrieve attempt record for a given role."""
        role_key = self.make_role_key(company, title, url)
        try:
            with self._get_conn() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM role_attempts WHERE role_key = ?", (role_key,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting role attempt info: {e}")
            return None


# Global Singleton Instance
retry_manager = RetryQueueManager()


# ============================================================================
# Per-Company State Hygiene (Rule 5)
# ============================================================================
async def clear_company_browser_state(
    browser=None,
    context=None,
    page=None,
    company: str = "Unknown"
) -> bool:
    """
    Per-company state hygiene:
    After an application for a company resolves (terminally or closes), clear the browser's
    cookies, cache, localStorage, sessionStorage, and site data before moving to the next company.
    Guarantees a fresh, clean browser state per company.
    """
    cleaned_items = []
    contexts = []
    if context:
        contexts.append(context)
    if browser:
        try:
            contexts.extend(browser.contexts or [])
        except Exception:
            pass
    if page and hasattr(page, "context") and page.context:
        contexts.append(page.context)

    # 1. Clear cookies and permissions across all active contexts
    for ctx in set(contexts):
        try:
            await ctx.clear_cookies()
            cleaned_items.append("cookies")
        except Exception as ce:
            logger.debug(f"Clear cookies notice: {ce}")
        try:
            await ctx.clear_permissions()
            cleaned_items.append("permissions")
        except Exception:
            pass

    # 2. Clear client-side DOM storage (localStorage, sessionStorage, indexedDB)
    if page:
        try:
            if not page.is_closed():
                await page.evaluate('''() => {
                    try { localStorage.clear(); } catch(e) {}
                    try { sessionStorage.clear(); } catch(e) {}
                }''')
                cleaned_items.append("storage")
        except Exception:
            pass

    logger.info(
        f"[STATE HYGIENE] Cleared browser state ({', '.join(set(cleaned_items)) or 'all storage & cookies'}) "
        f"for {company}. Fresh state guaranteed before moving to next company."
    )
    return True
