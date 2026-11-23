"""
AutoApply - Per-Company Application Cap Tracker
Enforces per-company application limits across candidate submission windows (e.g. max 2 applications per 180 days).
Tracks and records employer candidate limits (e.g. OpenAI 5 apps per 180 days).
"""
import os
import re
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Tuple, Optional
from urllib.parse import urlparse

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

DB_PATH = os.path.join(settings.DATA_DIR, "autoapply.db")


# Specific employer candidate-limit rejections (never broad substring matching)
CANDIDATE_LIMIT_PATTERNS = [
    re.compile(r"maximum\s+(?:number\s+of\s+)?active\s+applications", re.IGNORECASE),
    re.compile(r"reached\s+(?:the|your)\s+(?:maximum\s+)?application\s+limit", re.IGNORECASE),
    re.compile(r"applied\s+to\s+the\s+maximum\s+number\s+of", re.IGNORECASE),
    re.compile(r"exceeded\s+the\s+(?:allowed\s+)?(?:number\s+of\s+)?applications", re.IGNORECASE),
    re.compile(r"limit\s+of\s+\d+\s+applications\s+per", re.IGNORECASE),
    re.compile(r"may\s+not\s+apply\s+more\s+than\s+\d+\s+times\s+in\s+any\s+\d+\s+day", re.IGNORECASE),
    re.compile(r"candidate\s+application\s+limit", re.IGNORECASE),
]


def is_employer_candidate_limit(text: str) -> bool:
    """Check if verification rejection text matches an employer candidate limit policy."""
    if not text:
        return False
    return any(p.search(text) for p in CANDIDATE_LIMIT_PATTERNS)


def normalize_company_name(name: str) -> str:
    """Normalize company name to canonical lowercase form."""
    if not name:
        return ""
    # Strip common corporate suffixes
    clean = re.sub(r'\b(inc\.?|llc\.?|corp\.?|corporation|ltd\.?|limited|co\.?|technologies|technology|ai)\b', '', name, flags=re.IGNORECASE)
    clean = re.sub(r'[^a-zA-Z0-9\s]', ' ', clean)
    return " ".join(clean.lower().split())


def extract_ats_domain(url: str) -> str:
    """Extract ATS provider domain and tenant/company slug from URL."""
    if not url:
        return ""
    try:

        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        
        # Greenhouse: boards.greenhouse.io/{company} or boards-api.greenhouse.io/v1/boards/{company} or job-boards.greenhouse.io/embed/job_app?for={company}
        if "greenhouse.io" in netloc:
            from urllib.parse import parse_qs
            qs = parse_qs(parsed.query)
            if "for" in qs and qs["for"]:
                return f"greenhouse:{qs['for'][0].lower()}"
            if len(path_parts) >= 1 and path_parts[0] != "v1" and path_parts[0] != "embed":
                return f"greenhouse:{path_parts[0].lower()}"
            elif len(path_parts) >= 4 and path_parts[0] == "v1":
                return f"greenhouse:{path_parts[3].lower()}"
            return "greenhouse"

        # Lever: jobs.lever.co/{company}
        if "lever.co" in netloc:
            if len(path_parts) >= 1:
                return f"lever:{path_parts[0].lower()}"
            return "lever"

        # Ashby: jobs.ashbyhq.com/{company}
        if "ashbyhq.com" in netloc:
            if len(path_parts) >= 1:
                return f"ashby:{path_parts[0].lower()}"
            return "ashby"

        # Fallback to domain
        return netloc
    except Exception:
        return ""


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


class CompanyCapTracker:
    """Manages per-company submission limits and employer-enforced application caps."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_tables()

    def _get_connection(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return ClosingConnection(conn)

    def _init_tables(self):
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS company_submissions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_norm TEXT NOT NULL,
                        company_raw TEXT NOT NULL,
                        ats_domain TEXT NOT NULL,
                        role_title TEXT,
                        job_url TEXT NOT NULL,
                        applied_at TIMESTAMP NOT NULL,
                        status TEXT NOT NULL,
                        match_reason TEXT
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS employer_caps (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_norm TEXT NOT NULL UNIQUE,
                        ats_domain TEXT NOT NULL,
                        capped_until TIMESTAMP NOT NULL,
                        reason TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS company_circuit_breaker (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_norm TEXT NOT NULL,
                        ats_domain TEXT NOT NULL,
                        halt_reason TEXT NOT NULL,
                        created_at TIMESTAMP NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS company_pauses (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_norm TEXT NOT NULL UNIQUE,
                        ats_domain TEXT NOT NULL,
                        paused_until TIMESTAMP NOT NULL,
                        reasons TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_comp_norm ON company_submissions(company_norm)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_comp_ats ON company_submissions(ats_domain)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_comp_applied ON company_submissions(applied_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_circuit_comp ON company_circuit_breaker(company_norm)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_circuit_created ON company_circuit_breaker(created_at)")
                conn.commit()
            self._seed_from_history()
        except Exception as e:
            logger.error(f"Error initializing company cap tables: {e}")

    def _seed_from_history(self):
        """Seed company submissions and known employer limits from historical CSV."""
        csv_path = os.path.expanduser("~/Desktop/job_applications.csv")
        if not os.path.exists(csv_path):
            return
        try:
            import csv
            with self._get_connection() as conn:
                count = conn.execute("SELECT COUNT(*) FROM company_submissions").fetchone()[0]
                if count == 0:
                    with open(csv_path, "r", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            comp = row.get("company", "").strip()
                            url = row.get("url", "").strip()
                            ts = row.get("timestamp", "").strip()
                            status = row.get("status", "submitted").strip()
                            if not comp or not url:
                                continue
                            norm_company = normalize_company_name(comp)
                            ats_domain = extract_ats_domain(url)
                            try:
                                applied_at = datetime.fromisoformat(ts).astimezone(timezone.utc).isoformat()
                            except Exception:
                                applied_at = datetime.now(timezone.utc).isoformat()
                            conn.execute("""
                                INSERT INTO company_submissions
                                (company_norm, company_raw, ats_domain, role_title, job_url, applied_at, status, match_reason)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """, (norm_company, comp, ats_domain, "Software Engineer", url, applied_at, status, "Historical Import"))
                    conn.commit()
                    logger.info("Successfully seeded company_submissions from Desktop/job_applications.csv")
        except Exception as e:
            logger.debug(f"Error seeding company submissions from history: {e}")

    def record_terminal_halt(self, company: str, url: str = "", reason: str = ""):
        """
        Record a terminal halt or CHALLENGE_BLOCKED event for company circuit breaker tracking.
        If 2 attempts for the same company end in terminal halt / CHALLENGE_BLOCKED within 24 hours:
        pauses NEW launches for that company for 24 hours and logs COMPANY_PAUSED.
        The pause does NOT consume cap slots and does NOT block already-queued submissions.
        """
        norm_company = normalize_company_name(company)
        ats_domain = extract_ats_domain(url)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        cutoff_iso = (now - timedelta(hours=24)).isoformat()
        reason_str = str(reason or "TERMINAL_HALT").strip()

        try:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT INTO company_circuit_breaker (company_norm, ats_domain, halt_reason, created_at)
                    VALUES (?, ?, ?, ?)
                """, (norm_company, ats_domain, reason_str, now_iso))
                conn.commit()

                # Check halts in rolling 24-hour window
                rows = conn.execute("""
                    SELECT halt_reason, created_at FROM company_circuit_breaker
                    WHERE (company_norm = ? OR (ats_domain != '' AND ats_domain = ?))
                      AND created_at >= ?
                    ORDER BY created_at DESC
                """, (norm_company, ats_domain, cutoff_iso)).fetchall()

                if len(rows) >= 2:
                    r1 = rows[0]["halt_reason"]
                    r2 = rows[1]["halt_reason"]
                    paused_until_dt = now + timedelta(hours=24)
                    paused_until_iso = paused_until_dt.isoformat()
                    reasons_str = f"{r1} | {r2}"
                    conn.execute("""
                        INSERT OR REPLACE INTO company_pauses (company_norm, ats_domain, paused_until, reasons)
                        VALUES (?, ?, ?, ?)
                    """, (norm_company, ats_domain, paused_until_iso, reasons_str))
                    conn.commit()
                    log_msg = f"COMPANY_PAUSED: {company} paused for 24h until {paused_until_iso} due to 2 terminal halts within 24h (Reasons: '{r1}', '{r2}')"
                    logger.warning(log_msg)
                    print(log_msg, flush=True)
        except Exception as e:
            logger.error(f"Error recording company circuit breaker halt: {e}")

    def is_company_paused(self, company: str, url: str = "") -> Tuple[bool, str]:
        """
        Check if the company is currently paused by the circuit breaker (2 halts within 24h).
        Returns: (is_paused: bool, reason: str)
        """
        norm_company = normalize_company_name(company)
        ats_domain = extract_ats_domain(url)
        now = datetime.now(timezone.utc)

        try:
            with self._get_connection() as conn:
                row = conn.execute("""
                    SELECT paused_until, reasons FROM company_pauses
                    WHERE company_norm = ? OR (ats_domain != '' AND ats_domain = ?)
                """, (norm_company, ats_domain)).fetchone()
                if row:
                    paused_until_str = row["paused_until"]
                    try:
                        dt = datetime.fromisoformat(paused_until_str)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if now < dt:
                            hours_left = max(1, int((dt - now).total_seconds() / 3600))
                            msg = f"COMPANY_PAUSED: New launches paused for {company} (~{hours_left}h remaining): {row['reasons']}"
                            return True, msg
                    except Exception as pe:
                        logger.debug(f"Error parsing paused_until in is_company_paused: {pe}")
        except Exception as e:
            logger.debug(f"Error checking company pause: {e}")
        return False, ""

    def is_company_capped(self, company: str, url: str = "") -> Tuple[bool, str, int]:
        """
        Check if submitting to this company would violate the per-company cap or employer-enforced limit.
        Returns: (is_capped: bool, reason: str, prior_count: int)
        """
        norm_company = normalize_company_name(company)
        ats_domain = extract_ats_domain(url)
        now = datetime.now(timezone.utc)

        try:
            with self._get_connection() as conn:
                # 0. Check circuit breaker pause (2 terminal halts within 24h)
                is_paused, pause_reason = self.is_company_paused(company, url)
                if is_paused:
                    return True, pause_reason, 0

                # 1. Check employer-enforced limit
                row = conn.execute(
                    "SELECT capped_until, reason FROM employer_caps WHERE company_norm = ?",
                    (norm_company,)
                ).fetchone()
                if row:
                    try:
                        capped_until = datetime.fromisoformat(row["capped_until"])
                        if capped_until.tzinfo is None:
                            capped_until = capped_until.replace(tzinfo=timezone.utc)
                        if now < capped_until:
                            days_left = (capped_until - now).days
                            return True, f"Employer candidate limit active for {company} (locked for ~{days_left} more days: {row['reason']})", 0
                    except Exception as pe:
                        logger.debug(f"Error parsing capped_until timestamp: {pe}")

                # 2. Check 1-hour cooldown per company (or company ATS domain)
                cooldown_hours = getattr(settings, "COMPANY_COOLDOWN_HOURS", 1)
                cooldown_cutoff = (now - timedelta(hours=cooldown_hours)).isoformat()
                recent = conn.execute("""
                    SELECT applied_at, role_title, status FROM company_submissions
                    WHERE (company_norm = ? OR (ats_domain != '' AND ats_domain = ?))
                      AND status = 'submitted'
                      AND applied_at >= ?
                    ORDER BY applied_at DESC LIMIT 1
                """, (norm_company, ats_domain, cooldown_cutoff)).fetchone()
                if recent:
                    try:
                        last_dt = datetime.fromisoformat(recent["applied_at"])
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        mins_ago = max(1, int((now - last_dt).total_seconds() / 60))
                    except Exception:
                        mins_ago = 1
                    remaining_mins = max(1, int(cooldown_hours * 60 - mins_ago))
                    return True, f"Per-company 1-hour cooldown active for {company} (last application was {mins_ago}m ago; limit is 1 application per company per hour).", 1

                # 3. Check windowed application count
                window_days = getattr(settings, "COMPANY_CAP_WINDOW_DAYS", 180)
                max_apps = getattr(settings, "MAX_APPLICATIONS_PER_COMPANY", 2)
                cutoff = (now - timedelta(days=window_days)).isoformat()

                # Match on normalized company name OR same ATS tenant domain
                query = """
                    SELECT COUNT(*) as count FROM company_submissions
                    WHERE (company_norm = ? OR (ats_domain != '' AND ats_domain = ?))
                      AND applied_at >= ?
                      AND status = 'submitted'
                """
                res = conn.execute(query, (norm_company, ats_domain, cutoff)).fetchone()
                prior_count = res["count"] if res else 0

                if prior_count >= max_apps:
                    return True, f"Per-company cap reached: already applied {prior_count}/{max_apps} times to {company} in the last {window_days} days.", prior_count

                return False, f"Under cap ({prior_count}/{max_apps} applications in window)", prior_count

        except Exception as e:
            logger.error(f"Error checking company cap: {e}")
            return False, "Error checking cap", 0

    def record_submission(self, company: str, role_title: str, job_url: str, status: str = "submitted", match_reason: str = ""):
        """Record an application submission for company cap tracking."""
        norm_company = normalize_company_name(company)
        ats_domain = extract_ats_domain(job_url)
        now_iso = datetime.now(timezone.utc).isoformat()

        status_clean = str(status).strip().lower()
        # Cap accounting: The 2-per-180-day cap counts ONLY verified submissions.
        # Forms that died mid-fill, CHALLENGE_BLOCKED, FAILED, or MISSING_INFO must NOT consume cap budget.
        if status_clean != "submitted":
            if "fail" in status_clean or "block" in status_clean or "challenge" in status_clean or "missing" in status_clean or "skip" in status_clean or "unverified" in status_clean:
                status_clean = "submit_failed"
            # Record terminal halt / challenge block for circuit breaker
            self.record_terminal_halt(company, job_url, match_reason or status)

        try:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT INTO company_submissions 
                    (company_norm, company_raw, ats_domain, role_title, job_url, applied_at, status, match_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (norm_company, company, ats_domain, role_title, job_url, now_iso, status_clean, match_reason))
                conn.commit()
                logger.info(f"Recorded submission to {company} ({role_title}) [status={status_clean}] for company cap tracking.")
        except Exception as e:
            logger.error(f"Error recording company submission: {e}")

    def record_employer_limit_reached(self, company: str, url: str = "", limit_days: int = 180, reason: str = "Candidate application limit reached"):
        """Record that employer rejects submissions due to candidate quotas (e.g. OpenAI 5 apps per 180 days)."""
        norm_company = normalize_company_name(company)
        ats_domain = extract_ats_domain(url)
        until_iso = (datetime.now(timezone.utc) + timedelta(days=limit_days)).isoformat()

        try:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO employer_caps (company_norm, ats_domain, capped_until, reason)
                    VALUES (?, ?, ?, ?)
                """, (norm_company, ats_domain, until_iso, reason))
                conn.commit()
                logger.warning(f"Recorded employer limit cap for {company}: locked for {limit_days} days until {until_iso}.")
        except Exception as e:
            logger.error(f"Error recording employer cap: {e}")

    def get_daily_application_count(self) -> int:
        """Get the number of applications submitted in the rolling 24-hour window."""
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=24)).isoformat()
        try:
            with self._get_connection() as conn:
                res = conn.execute(
                    "SELECT COUNT(*) as count FROM company_submissions WHERE applied_at >= ? AND status = 'submitted'",
                    (cutoff,)
                ).fetchone()
                return res["count"] if res else 0
        except Exception as e:
            logger.debug(f"Error querying daily application count: {e}")
            return 0

    def get_last_submission_time(self) -> Optional[datetime]:
        """Get the timestamp of the most recent submitted application."""
        try:
            with self._get_connection() as conn:
                res = conn.execute(
                    "SELECT applied_at FROM company_submissions WHERE status = 'submitted' ORDER BY applied_at DESC LIMIT 1"
                ).fetchone()
                if res and res["applied_at"]:
                    dt = datetime.fromisoformat(res["applied_at"])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
        except Exception as e:
            logger.debug(f"Error querying last submission time: {e}")
        return None

    def get_employer_lockout(self, company: str) -> Optional[Tuple[datetime, str]]:
        """
        Check if an employer candidate limit lockout is active for this company.
        Returns (capped_until: datetime, reason: str) or None if not locked.
        """
        norm_company = normalize_company_name(company)
        now = datetime.now(timezone.utc)
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT capped_until, reason FROM employer_caps WHERE company_norm = ?",
                    (norm_company,)
                ).fetchone()
                if row:
                    try:
                        capped_until = datetime.fromisoformat(row["capped_until"])
                        if capped_until.tzinfo is None:
                            capped_until = capped_until.replace(tzinfo=timezone.utc)
                        if now < capped_until:
                            return capped_until, row["reason"]
                    except Exception as pe:
                        logger.debug(f"Error parsing capped_until timestamp: {pe}")
        except Exception as e:
            logger.error(f"Error checking employer lockout: {e}")
        return None

    def reset_cap(self, company: str):
        """Reset submission records, employer caps, and circuit breaker pauses for a company (useful for tests)."""
        norm = normalize_company_name(company)
        try:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM company_submissions WHERE company_norm = ?", (norm,))
                conn.execute("DELETE FROM employer_caps WHERE company_norm = ?", (norm,))
                conn.execute("DELETE FROM company_circuit_breaker WHERE company_norm = ?", (norm,))
                conn.execute("DELETE FROM company_pauses WHERE company_norm = ?", (norm,))
                conn.commit()
        except Exception as e:
            logger.error(f"Error resetting company cap: {e}")


# Singleton instance
company_cap_tracker = CompanyCapTracker()
