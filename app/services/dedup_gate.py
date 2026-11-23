"""
Dedup Gate Service (FIX 1: Evaluated vs Actioned Split)
Integrates with ~/Desktop/jobs_seen.db to enforce:
1. Permanently suppress actioned jobs (applied, alerted, cap_skipped_after_match)
2. Re-admit rejected jobs if gate_version < CURRENT_GATE_VERSION (2) or older than 30 days
3. Canonical dedup key strictly (company, canonical_url)
"""
import os
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Tuple

logger = logging.getLogger(__name__)

JOBS_SEEN_DB_PATH = os.getenv("JOBS_SEEN_DB_PATH", os.path.expanduser("~/Desktop/jobs_seen.db"))
CURRENT_GATE_VERSION = 2
REEVALUATION_DAYS = 30


def normalize_url(url: str) -> str:
    """Strip tracking tokens, query params and fragments to canonicalize URL."""
    if not url:
        return ""
    base = url.split("?")[0].split("#")[0].rstrip("/")
    return base.lower().strip()


def make_job_key(company: str, url: str) -> str:
    """Canonical dedup key: (company, canonical_url)."""
    clean_comp = company.strip().lower() if company else "unknown"
    canon_url = normalize_url(url)
    return f"{clean_comp}|{canon_url}"


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


class DedupGate:
    """Deduplication evaluation engine."""

    @staticmethod
    def evaluate_job(company: str, url: str, db_path: str = JOBS_SEEN_DB_PATH) -> Tuple[str, str]:
        """
        Evaluate dedup status against jobs_seen.db.
        Returns tuple (status, reason) where status is one of:
          - 'actioned': Permanently suppressed (applied, alerted, cap-skipped after match)
          - 'stale_rejected': Suppressed (rejected under current gate version within 30 days)
          - 're_admitted': Re-admitted for evaluation (rejected under older gate or >30 days ago)
          - 'new': Completely unseen job
        """
        if not url or not os.path.exists(db_path):
            return "new", "no_db_or_empty_url"

        job_key = make_job_key(company, url)
        try:
            with ClosingConnection(sqlite3.connect(db_path)) as conn:
                cursor = conn.cursor()
                # 1. Actioned check (permanently suppressed ONLY for verified applied/submitted)
                cursor.execute("SELECT 1 FROM actioned_jobs WHERE job_key = ? AND action IN ('applied', 'submitted') LIMIT 1", (job_key,))
                if cursor.fetchone():
                    return "actioned", "Permanently suppressed (already applied/submitted)"

                # 2. Seen jobs check
                cursor.execute(
                    "SELECT gate_version, last_evaluated_at, outcome, reason FROM seen_jobs WHERE job_key = ? LIMIT 1",
                    (job_key,)
                )
                row = cursor.fetchone()
                if row:
                    gate_ver, last_eval, outcome, reason = row
                    if outcome == "actioned":
                        if reason in ("applied", "submitted"):
                            return "actioned", "Permanently suppressed (actioned status in seen_jobs)"
                        # If previously recorded as actioned with non-applied reason, allow re-evaluation below

                    if outcome in ("rejected", "skipped"):
                        if gate_ver is not None and gate_ver < CURRENT_GATE_VERSION:
                            return "re_admitted", f"Re-admitted: gate_version {gate_ver} < {CURRENT_GATE_VERSION}"

                        if last_eval:
                            try:
                                eval_str = str(last_eval).replace("Z", "+00:00")
                                if " " in eval_str and "+" not in eval_str:
                                    eval_dt = datetime.strptime(eval_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                                else:
                                    eval_dt = datetime.fromisoformat(eval_str)
                                    if eval_dt.tzinfo is None:
                                        eval_dt = eval_dt.replace(tzinfo=timezone.utc)
                                age_days = (datetime.now(timezone.utc) - eval_dt).days
                                if age_days >= REEVALUATION_DAYS:
                                    return "re_admitted", f"Re-admitted: evaluated {age_days}d ago (>= {REEVALUATION_DAYS}d)"
                            except Exception as dte:
                                logger.debug(f"Date parse error in DedupGate: {dte}")

                        return "stale_rejected", f"Suppressed: rejected under gate_v{gate_ver} within {REEVALUATION_DAYS}d"

                return "new", "unseen"
        except Exception as e:
            logger.error(f"Error checking DedupGate for {job_key}: {e}")
            return "new", f"error: {e}"

    @staticmethod
    def mark_evaluated(company: str, url: str, title: str, outcome: str = "rejected", reason: str = "filter_skipped", gate_version: int = CURRENT_GATE_VERSION, db_path: str = JOBS_SEEN_DB_PATH):
        if not url or not os.path.exists(db_path):
            return
        job_key = make_job_key(company, url)
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with ClosingConnection(sqlite3.connect(db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO seen_jobs (job_key, company, url, title, first_seen_at, last_evaluated_at, gate_version, outcome, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_key) DO UPDATE SET
                        last_evaluated_at = excluded.last_evaluated_at,
                        gate_version = excluded.gate_version,
                        outcome = excluded.outcome,
                        reason = excluded.reason
                """, (job_key, company or "Unknown", url, title or "Unknown", now_iso, now_iso, gate_version, outcome, reason))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to record evaluated in DedupGate: {e}")

    @staticmethod
    def mark_actioned(company: str, url: str, title: str, action: str = "applied", db_path: str = JOBS_SEEN_DB_PATH):
        if not url or not os.path.exists(db_path):
            return
        if action not in ("applied", "submitted"):
            # Non-applied outcomes (cap skips, alerts) must never permanently burn or suppress postings
            return DedupGate.mark_evaluated(company, url, title, outcome="skipped", reason=action, db_path=db_path)
        job_key = make_job_key(company, url)
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with ClosingConnection(sqlite3.connect(db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO actioned_jobs (job_key, company, url, title, actioned_at, action)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (job_key, company or "Unknown", url, title or "Unknown", now_iso, action))
                cursor.execute("""
                    INSERT INTO seen_jobs (job_key, company, url, title, first_seen_at, last_evaluated_at, gate_version, outcome, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'actioned', ?)
                    ON CONFLICT(job_key) DO UPDATE SET
                        last_evaluated_at = excluded.last_evaluated_at,
                        outcome = 'actioned',
                        reason = excluded.reason
                """, (job_key, company or "Unknown", url, title or "Unknown", now_iso, now_iso, CURRENT_GATE_VERSION, action))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to record actioned in DedupGate: {e}")
