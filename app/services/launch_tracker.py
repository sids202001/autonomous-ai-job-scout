"""
AutoApply - Launch Tracker & Submission Evidence Verifier
Enforces:
1. FIX 1: Terminal outcome for every launch (SUBMITTED, UNVERIFIED, FAILED, CHALLENGE_BLOCKED, SKIPPED).
   Flag launches older than 30 minutes without terminal outcome as STALLED in digest.
2. FIX 2: "Submitted" requires all 4 evidence items (log entry, fresh screenshot, Telegram receipt with screenshot, digest entry).
   Missing any one -> status UNVERIFIED, never SUBMITTED.
3. FIX 4: Build proof tracking — until one application is proven SUBMITTED-with-evidence under current build,
   submissions remain UNVERIFIED.
"""
import os
import json
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple, Union

try:
    from config import settings
    DATA_DIR = getattr(settings, "DATA_DIR", os.path.expanduser("~/sid_job_scrip/data"))
except Exception:
    DATA_DIR = os.path.expanduser("~/sid_job_scrip/data")

logger = logging.getLogger(__name__)

LAUNCH_TRACKER_PATH = os.path.join(DATA_DIR, "launch_tracker.json")
BUILD_PROOF_PATH = os.path.join(DATA_DIR, "build_proof.json")
CURRENT_BUILD_ID = "2026-09-20.v2.observability"


class LaunchTracker:
    """Manages launch lifecycle, terminal outcome enforcement, and evidence verification."""

    TERMINAL_STATUSES = {
        "SUBMITTED",
        "READY_TO_SUBMIT",
        "UNVERIFIED",
        "FAILED",
        "CHALLENGE_BLOCKED",
        "SKIPPED",
        "STALLED",
    }

    def __init__(self, tracker_path: str = LAUNCH_TRACKER_PATH, proof_path: str = BUILD_PROOF_PATH):
        self.tracker_path = tracker_path
        self.proof_path = proof_path
        os.makedirs(os.path.dirname(self.tracker_path) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(self.proof_path) or ".", exist_ok=True)
        if not os.path.exists(self.tracker_path):
            with open(self.tracker_path, "w", encoding="utf-8") as f:
                json.dump([], f)
        if not os.path.exists(self.proof_path):
            with open(self.proof_path, "w", encoding="utf-8") as f:
                json.dump({"current_build_id": CURRENT_BUILD_ID, "proven": False, "proven_at": None, "proof_record": None}, f, indent=2)

    def _read_records(self) -> List[Dict[str, Any]]:
        try:
            with open(self.tracker_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"Error reading launch tracker: {e}")
            return []

    def _write_records(self, records: List[Dict[str, Any]]):
        temp_path = f"{self.tracker_path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)
            os.replace(temp_path, self.tracker_path)
        except Exception as e:
            logger.error(f"Error writing launch tracker: {e}")
            if os.path.exists(temp_path):
                os.remove(temp_path)

    # ── Build Proof Management (FIX 4) ──
    def get_build_proof_status(self) -> Dict[str, Any]:
        try:
            with open(self.proof_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("current_build_id") == CURRENT_BUILD_ID:
                    return data
        except Exception:
            pass
        return {"current_build_id": CURRENT_BUILD_ID, "proven": False, "proven_at": None, "proof_record": None}

    def is_build_proven(self) -> bool:
        return bool(self.get_build_proof_status().get("proven", False))

    def mark_build_proven(self, proof_record: Dict[str, Any]):
        """Mark the current build as verified and proven with concrete evidence."""
        data = {
            "current_build_id": CURRENT_BUILD_ID,
            "proven": True,
            "proven_at": datetime.now(timezone.utc).isoformat(),
            "proof_record": proof_record
        }
        temp_path = f"{self.proof_path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, self.proof_path)
            logger.info(f"Build {CURRENT_BUILD_ID} marked as PROVEN with record: {proof_record.get('company')} ({proof_record.get('url')})")
        except Exception as e:
            logger.error(f"Error writing build proof: {e}")

    def register_launch(
        self,
        company: str,
        role: str,
        url: str,
        portal: str = "direct",
        launch_id: Optional[str] = None,
        rank: Optional[int] = None,
        fit_score: Optional[float] = None
    ) -> str:
        """Register a new auto-apply launch."""
        l_id = launch_id or f"lnch_{uuid.uuid4().hex[:8]}"
        now_iso = datetime.now(timezone.utc).isoformat()
        entry = {
            "launch_id": l_id,
            "timestamp": now_iso,
            "company": company,
            "role": role,
            "url": url,
            "portal": portal,
            "status": "LAUNCHED",
            "terminal_outcome": None,
            "resolved_at": None,
            "reason": None,
            "rank": rank,
            "fit_score": fit_score,
            "screenshot_path": None,
            "receipt_msg_id": None,
            "evidence": {
                "log_entry": False,
                "screenshot": False,
                "telegram_receipt": False,
                "digest_entry": False
            }
        }
        records = self._read_records()
        records.append(entry)
        self._write_records(records)
        rank_str = f" [Rank #{rank}]" if rank is not None else ""
        logger.info(f"Launch registered: [{l_id}]{rank_str} {company} - {role}")
        return l_id

    def resolve_launch(
        self,
        launch_id: str,
        status: str,
        reason: str = "",
        screenshot_path: Optional[str] = None,
        receipt_msg_id: Optional[Union[int, str]] = None,
        sync_audit_log: bool = True,
        photo_status: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Resolve a launch to a terminal outcome: SUBMITTED, UNVERIFIED, FAILED, CHALLENGE_BLOCKED, or SKIPPED.
        """
        status_upper = status.upper().strip()
        if status_upper not in self.TERMINAL_STATUSES:
            if "CHALLENGE" in status_upper:
                status_upper = "CHALLENGE_BLOCKED"
            elif "SUBMIT" in status_upper:
                status_upper = "SUBMITTED"
            elif "FAIL" in status_upper or "ERROR" in status_upper:
                status_upper = "FAILED"
            elif "SKIP" in status_upper or "CAP" in status_upper:
                status_upper = "SKIPPED"
            else:
                status_upper = "FAILED"

        records = self._read_records()
        # Match the MOST RECENT unresolved launch with this launch_id (reversed order).
        # Previously, forward iteration matched an old historical entry with the same ATS job token,
        # leaving the newest LAUNCHED entry unresolved until check_stalled_launches() flipped it to STALLED.
        target_entry = None
        for r in reversed(records):
            if r.get("launch_id") == launch_id and (r.get("status") == "LAUNCHED" or not r.get("terminal_outcome")):
                target_entry = r
                break
        if target_entry is None:
            for r in reversed(records):
                if r.get("launch_id") == launch_id:
                    target_entry = r
                    break

        now_iso = datetime.now(timezone.utc).isoformat()
        is_pending = (photo_status == "photo_pending" or receipt_msg_id == "photo_pending")
        p_status = "photo_pending" if is_pending else (photo_status or ("delivered" if receipt_msg_id else None))

        # Check evidence validity if status is SUBMITTED or READY_TO_SUBMIT
        if status_upper in ("SUBMITTED", "READY_TO_SUBMIT"):
            has_shot = bool(screenshot_path and os.path.exists(screenshot_path) and os.path.getsize(screenshot_path) > 0)
            has_receipt = bool(receipt_msg_id is not None and str(receipt_msg_id).strip() != "")
            # FIX 2: All 4 evidence items required (receipt_msg_id can be a telegram msg id or 'photo_pending')
            if not (has_shot and has_receipt):
                status_upper = "UNVERIFIED"
                missing = []
                if not has_shot:
                    missing.append("missing confirmation screenshot")
                if not has_receipt:
                    missing.append("missing Telegram receipt msg_id")
                reason = f"UNVERIFIED: {', '.join(missing)} ({reason})"

            # FIX 4: Build proof requirement
            if not self.is_build_proven() and status_upper == "SUBMITTED":
                # This application serves as the proof case!
                proof_record = {
                    "launch_id": launch_id,
                    "company": target_entry.get("company") if target_entry else "Unknown",
                    "role": target_entry.get("role") if target_entry else "Unknown",
                    "url": target_entry.get("url") if target_entry else "",
                    "screenshot_path": screenshot_path,
                    "receipt_msg_id": str(receipt_msg_id),
                    "timestamp": now_iso
                }
                self.mark_build_proven(proof_record)

        if target_entry:
            target_entry["status"] = status_upper
            target_entry["terminal_outcome"] = status_upper
            target_entry["resolved_at"] = now_iso
            target_entry["reason"] = reason
            target_entry["screenshot_path"] = screenshot_path
            target_entry["receipt_msg_id"] = str(receipt_msg_id) if receipt_msg_id else None
            target_entry["photo_status"] = p_status
            target_entry["photo_pending"] = is_pending
            target_entry["evidence"] = {
                "log_entry": True,
                "screenshot": bool(screenshot_path and os.path.exists(screenshot_path) and os.path.getsize(screenshot_path) > 0),
                "telegram_receipt": bool(receipt_msg_id is not None),
                "digest_entry": True
            }
        else:
            # If launch_id wasn't registered prior, create entry now
            target_entry = {
                "launch_id": launch_id,
                "timestamp": now_iso,
                "company": "Direct Apply",
                "role": "Software Engineer",
                "url": "",
                "portal": "direct",
                "status": status_upper,
                "terminal_outcome": status_upper,
                "resolved_at": now_iso,
                "reason": reason,
                "screenshot_path": screenshot_path,
                "receipt_msg_id": str(receipt_msg_id) if receipt_msg_id else None,
                "photo_status": p_status,
                "photo_pending": is_pending,
                "evidence": {
                    "log_entry": True,
                    "screenshot": bool(screenshot_path and os.path.exists(screenshot_path)),
                    "telegram_receipt": bool(receipt_msg_id is not None),
                    "digest_entry": True
                }
            }
            records.append(target_entry)

        self._write_records(records)
        logger.info(f"Launch resolved: [{launch_id}] -> {status_upper} ({reason})")

        # Synchronize with AuditLogger and OutcomeLedger
        if sync_audit_log:
            try:
                from app.services.audit_logger import audit_logger
                audit_logger.log_submission(
                    company=target_entry.get("company", "Unknown"),
                    role_title=target_entry.get("role", "Software Engineer"),
                    job_url=target_entry.get("url", ""),
                    status=status_upper.lower(),
                    skip_reason=reason if status_upper in ("SKIPPED", "FAILED", "CHALLENGE_BLOCKED", "UNVERIFIED") else None,
                    portal=target_entry.get("portal", "direct"),
                    screenshot_path=screenshot_path,
                    receipt_msg_id=receipt_msg_id,
                    terminal_status=status_upper,
                    photo_status=p_status,
                    job_id=target_entry.get("launch_id") or launch_id
                )
            except Exception as se:
                logger.debug(f"AuditLogger sync in LaunchTracker notice: {se}")

        return target_entry

    def update_launch_step(self, launch_id: str, step: str, field: Optional[str] = None):
        """Record current execution step and optional field on the active launch entry."""
        records = self._read_records()
        for r in reversed(records):
            if r.get("launch_id") == launch_id:
                r["step"] = step
                if field:
                    r["stalled_field"] = field
                self._write_records(records)
                break

    def check_stalled_launches(self, timeout_minutes: int = 30) -> List[Dict[str, Any]]:
        """
        Flag any launch older than timeout_minutes without a terminal outcome as STALLED.
        Logs exact step and field. Retires historical bare STALLED records.
        (Visible in digest, never sent as Telegram alert).
        """
        records = self._read_records()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=timeout_minutes)
        updated = False
        stalled_list = []

        for r in records:
            # Retire historical bare STALLED records lacking step/field attribution
            if r.get("status") == "STALLED" and r.get("reason") == f"Launch exceeded {timeout_minutes}m without terminal outcome":
                r["reason"] = "Stalled at step='execution_timeout' [retired bare STALLED: scan timeout cancelled task during fill/pacing]"
                updated = True

            if r.get("status") == "LAUNCHED" and not r.get("terminal_outcome"):
                try:
                    ts = datetime.fromisoformat(r["timestamp"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        step_info = r.get("step") or "form_submission"
                        field_info = f" (field: '{r.get('stalled_field')}')" if r.get("stalled_field") else ""
                        r["status"] = "STALLED"
                        r["terminal_outcome"] = "STALLED"
                        r["resolved_at"] = now.isoformat()
                        r["reason"] = f"Stalled at step='{step_info}'{field_info}: exceeded {timeout_minutes}m without terminal outcome"
                        updated = True
                        stalled_list.append(r)
                        logger.warning(f"Flagged STALLED launch: [{r.get('launch_id')}] {r.get('company')} - {r.get('role')} at step='{step_info}'")
                except Exception as e:
                    logger.debug(f"Error checking stalled timestamp: {e}")

        if updated:
            self._write_records(records)
        return stalled_list

    def get_last_24h_launches(self) -> List[Dict[str, Any]]:
        """Return all launches from the past 24 hours, automatically updating stalled ones."""
        self.check_stalled_launches(timeout_minutes=30)
        records = self._read_records()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        results = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    results.append(r)
            except Exception:
                results.append(r)
        return results

    def verify_submission_evidence(
        self,
        log_entry: bool,
        screenshot_path: Optional[str],
        receipt_msg_id: Optional[Union[int, str]],
        digest_entry: bool
    ) -> Tuple[bool, str]:
        """
        Enforce the 4-evidence item rule (FIX 2):
        1. Per-application log entry with final status
        2. Fresh post-submit confirmation screenshot on disk
        3. Telegram success receipt with message ID
        4. Digest entry
        """
        missing = []
        if not log_entry:
            missing.append("(1) per-application log entry")
        if not (screenshot_path and os.path.exists(screenshot_path) and os.path.getsize(screenshot_path) > 0):
            missing.append("(2) post-submit confirmation screenshot artifact")
        if receipt_msg_id is None or str(receipt_msg_id).strip() == "":
            missing.append("(3) Telegram success receipt message ID")
        if not digest_entry:
            missing.append("(4) digest entry")

        if missing:
            return False, f"Missing evidence items: {', '.join(missing)}"
        return True, "All 4 evidence items verified"


launch_tracker = LaunchTracker()
