"""
AutoApply - Intelligence Loop & Outcome Ledger
Learns from historical application outcomes, manages question memory, and surfaces
portal patterns without ever weakening safety or reliability guarantees.

Features:
1. Outcome Ledger: Records every application outcome (applied, skipped, challenge_blocked, cap_hit)
   per company, portal, and role family. Computes challenge rates and flags deprioritized portals.
2. Question Memory: Normalizes and persists newly observed screening questions with provenance.
   Ensures deterministic, instant resolution for subsequent identical questions. Never invents entries.
3. Relevance Feedback: Tracks applied role response rates vs silent roles for digest reporting.
   The bot NEVER alters its own relevance threshold.
4. Safety Supremacy: Learned behavior NEVER overrides Sections 1-5 or the 7 reliability guarantees.
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Union
from collections import defaultdict

logger = logging.getLogger(__name__)

LEDGER_PATH = "data/outcome_ledger.json"
LEARNED_QUESTIONS_PATH = "data/learned_fields.json"


def categorize_role_family(role_title: str) -> str:
    """Categorize job title into canonical role family."""
    t = (role_title or "").lower()
    if any(k in t for k in ["data", "etl", "pipeline", "analytics"]):
        return "Data Engineering"
    elif any(k in t for k in ["ai", "machine learning", "ml", "llm", "automation"]):
        return "AI/ML Engineering"
    elif any(k in t for k in ["backend", "python", "api", "microservice"]):
        return "Backend Engineering"
    elif any(k in t for k in ["software", "sde", "swe", "developer"]):
        return "Software Engineering"
    elif any(k in t for k in ["full stack", "fullstack"]):
        return "Full Stack Engineering"
    return "Engineering"


class OutcomeLedger:
    """
    Persistent outcome ledger recording every application attempt.
    """

    VALID_OUTCOMES = {"applied", "ready_to_submit", "skipped", "challenge_blocked", "cap_hit"}

    def __init__(self, storage_path: str = LEDGER_PATH):
        self.storage_path = storage_path
        os.makedirs(os.path.dirname(self.storage_path) or ".", exist_ok=True)
        if not os.path.exists(self.storage_path):
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump([], f)

    def _read_records(self) -> List[Dict[str, Any]]:
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"Error reading outcome ledger: {e}")
            return []

    def _write_records(self, records: List[Dict[str, Any]]):
        temp_path = f"{self.storage_path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)
            os.replace(temp_path, self.storage_path)
        except Exception as e:
            logger.error(f"Error writing outcome ledger: {e}")
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def record_outcome(
        self,
        company: str,
        role: str,
        portal: str,
        url: str,
        outcome: str,
        reason: str,
        stage: Optional[str] = None,
        screenshot_path: Optional[str] = None,
        receipt_msg_id: Optional[Union[int, str]] = None,
        terminal_status: Optional[str] = None,
        photo_status: Optional[str] = None,
        photo_pending: Optional[bool] = None,
        job_id: Optional[str] = None,
        title: Optional[str] = None,
        ats: Optional[str] = None,
        halt_reason: Optional[str] = None,
        screenshots: Optional[Union[str, List[str]]] = None
    ) -> Dict[str, Any]:
        """
        Record an application outcome in the persistent ledger.
        Guarantees presence of: job_id, company, title, ats, outcome, halt_reason, screenshots, timestamp.
        """
        outcome_norm = outcome.lower().strip()
        if outcome_norm not in self.VALID_OUTCOMES:
            # Map legacy or synonym names
            if "challenge" in outcome_norm or "captcha" in outcome_norm:
                outcome_norm = "challenge_blocked"
            elif "cap" in outcome_norm:
                outcome_norm = "cap_hit"
            elif outcome_norm in ("submitted", "verified"):
                outcome_norm = "applied"
            elif "ready" in outcome_norm:
                outcome_norm = "ready_to_submit"
            elif outcome_norm in ("unverified",):
                outcome_norm = "unverified"
            elif outcome_norm in ("failed", "error"):
                outcome_norm = "failed"
            elif outcome_norm in ("stalled",):
                outcome_norm = "stalled"
            else:
                outcome_norm = "skipped"

        role_val = title or role or "Candidate Matching Role"
        role_family = categorize_role_family(role_val)
        portal_norm = (ats or portal or "direct").lower().strip()
        t_status = terminal_status or ("SUBMITTED" if outcome_norm == "applied" else outcome_norm.upper())

        is_pending = bool(photo_pending or photo_status == "photo_pending" or receipt_msg_id == "photo_pending")
        p_status = "photo_pending" if is_pending else (photo_status or ("delivered" if receipt_msg_id else None))

        h_reason = halt_reason or reason
        ss_val = screenshots or screenshot_path
        if isinstance(ss_val, str):
            ss_list = [ss_val]
        elif isinstance(ss_val, list):
            ss_list = ss_val
        else:
            ss_list = []

        entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "job_id": job_id,
            "company": company,
            "title": role_val,
            "role": role_val,
            "role_family": role_family,
            "ats": portal_norm,
            "portal": portal_norm,
            "url": url,
            "outcome": outcome_norm,
            "terminal_status": t_status,
            "reason": h_reason,
            "halt_reason": h_reason,
            "stage": stage or "pre_flight",
            "evidence_screenshot": screenshot_path or (ss_list[0] if ss_list else None),
            "screenshots": ss_list,
            "receipt_msg_id": str(receipt_msg_id) if receipt_msg_id is not None else None,
            "photo_status": p_status,
            "photo_pending": is_pending,
        }

        records = self._read_records()
        records.append(entry)
        self._write_records(records)
        logger.info(f"OutcomeLedger recorded [{t_status}] {company} - {role_val} ({portal_norm})")
        return entry

    def get_last_24h_outcomes(self, hours: int = 24) -> List[Dict[str, Any]]:
        """Return outcome records from the last N hours."""
        records = self._read_records()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)
        recent = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    recent.append(r)
            except Exception:
                recent.append(r)
        return recent

    def get_portal_challenge_rates(self, days: int = 7) -> Dict[str, Dict[str, Any]]:
        """
        Compute challenge rate per portal over the past N days.
        Returns dict: portal -> {attempts, challenges, rate, is_deprioritized, note}
        """
        records = self._read_records()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)

        portal_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: {"attempts": 0, "challenges": 0})

        for r in records:
            try:
                ts = datetime.fromisoformat(r["timestamp"])
                if ts < cutoff:
                    continue
            except Exception:
                continue

            portal = r.get("portal", "unknown")
            portal_counts[portal]["attempts"] += 1
            if r.get("outcome") == "challenge_blocked":
                portal_counts[portal]["challenges"] += 1

        results = {}
        for p, cnt in portal_counts.items():
            att = cnt["attempts"]
            chal = cnt["challenges"]
            rate = (chal / att * 100.0) if att > 0 else 0.0
            is_deprioritized = (rate >= 25.0 and att >= 2)
            note = f"{p.title()} challenge rate {rate:.0f}% this week — deprioritized" if is_deprioritized else f"{p.title()} challenge rate {rate:.0f}%"
            results[p] = {
                "attempts": att,
                "challenges": chal,
                "rate": round(rate, 1),
                "is_deprioritized": is_deprioritized,
                "note": note,
            }

        return results

    def get_digest_patterns(self) -> List[str]:
        """Generate human-readable intelligence patterns for the end-of-run digest."""
        patterns = []
        portal_stats = self.get_portal_challenge_rates(days=7)
        for p, stat in portal_stats.items():
            if stat["is_deprioritized"]:
                patterns.append(f"• 🛡️ *{stat['note']}*")

        return patterns


class QuestionMemory:
    """
    Registry growth and deterministic question memory.
    Grows ONLY from observed screening questions with provenance; never invents entries.
    """

    def __init__(self, memory_file: str = LEARNED_QUESTIONS_PATH):
        self.memory_file = memory_file
        os.makedirs(os.path.dirname(self.memory_file) or ".", exist_ok=True)
        if not os.path.exists(self.memory_file):
            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump({}, f)

    def _read_memory(self) -> Dict[str, Any]:
        try:
            with open(self.memory_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_memory(self, data: Dict[str, Any]):
        temp_path = f"{self.memory_file}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, self.memory_file)
        except Exception as e:
            logger.error(f"Error saving learned questions: {e}")
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def learn_observed_question(
        self,
        raw_question: str,
        answer: str,
        provenance_key: str = "observed_screening",
        source: str = "form_interaction"
    ) -> bool:
        """
        Normalize and persist newly observed screening question.
        Returns True if a new question was added (growth event).
        """
        from app.services.presubmit_gate import normalize_question

        norm_q = normalize_question(raw_question)
        if not norm_q or not answer:
            return False

        memory = self._read_memory()
        is_new = norm_q not in memory

        memory[norm_q] = {
            "raw_question": raw_question,
            "answer": answer,
            "provenance_key": provenance_key,
            "source": source,
            "learned_at": datetime.now(timezone.utc).isoformat(),
        }

        self._write_memory(memory)

        if is_new:
            logger.info(f"QuestionMemory: Registered new observed question: '{raw_question}' -> '{answer}' (provenance: {provenance_key})")
        return is_new

    def resolve_question(self, raw_question: str) -> Optional[Dict[str, Any]]:
        """
        Deterministically resolve question from memory if previously observed.
        """
        from app.services.presubmit_gate import normalize_question

        norm_q = normalize_question(raw_question)
        memory = self._read_memory()
        return memory.get(norm_q)


class RelevanceFeedbackTracker:
    """
    Tracks application outcomes (response vs silent) without altering relevance thresholds.
    """

    @classmethod
    def get_response_stats(cls) -> Dict[str, Any]:
        """Compute response stats across recorded applications."""
        ledger = OutcomeLedger()
        records = ledger._read_records()
        
        family_stats = defaultdict(lambda: {"applied": 0, "interview": 0, "rejected": 0, "silent": 0})
        for r in records:
            if r.get("outcome") == "applied":
                family = r.get("role_family", "Engineering")
                family_stats[family]["applied"] += 1
                # Default status is silent until updated via webhook/tracker
                family_stats[family]["silent"] += 1

        return dict(family_stats)


# Singletons
outcome_ledger = OutcomeLedger()
question_memory = QuestionMemory()
relevance_feedback = RelevanceFeedbackTracker()
