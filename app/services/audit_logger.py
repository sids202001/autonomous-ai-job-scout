"""
AutoApply - Submission Review & Targeting Audit Logger
Appends every submission attempt to submissions.log with company, role title, job URL,
timestamp, match score/reason, and outcome.
Provides End-of-Run Telegram digest formatting for submitted vs skipped applications.
"""
import os
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple

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

SUBMISSIONS_LOG_PATH = os.path.join(settings.DATA_DIR, "submissions.log")


def get_daemon_rss_telemetry_line() -> Optional[str]:
    """One telemetry line only: log daemon RSS so footprint is visible over time."""
    try:
        import subprocess
        import sys
        pid = os.getpid()
        if not any("resume_bot" in arg for arg in sys.argv):
            try:
                out_pid = subprocess.check_output(['pgrep', '-f', 'resume_bot\\.py'], stderr=subprocess.DEVNULL).decode().strip()
                pids = [int(p) for p in out_pid.splitlines() if p.isdigit() and int(p) != os.getpid()]
                if pids:
                    pid = pids[0]
            except Exception:
                pass
        out = subprocess.check_output(['ps', '-o', 'rss=', '-p', str(pid)], stderr=subprocess.DEVNULL).decode().strip()
        mb = round(int(out) / 1024.0, 1)
        return f"• *Daemon Memory (RSS):* {mb} MB"
    except Exception:
        return None



class AuditLogger:
    """Handles persistent submission logging and run digest collation with bounded memory footprint."""

    def __init__(self, log_path: str = SUBMISSIONS_LOG_PATH):
        self.log_path = log_path
        self._current_run_submitted: List[Dict[str, Any]] = []
        self._current_run_skipped: List[Dict[str, Any]] = []
        self._current_run_challenges: List[Dict[str, Any]] = []
        self._total_submitted_count: int = 0
        self._total_skipped_count: int = 0
        self._total_challenges_count: int = 0
        self._latest_diagnostics_report: Optional[str] = None
        self._init_log()

    @staticmethod
    def unify_skip_reason(reason: Optional[str], company: Optional[str] = None) -> str:
        """
        Unify skip reasons from diverse code paths into a single consistent template:
        <CATEGORY>: <detail>
        """
        if not reason:
            return "FILTERED: Policy criteria not met"

        reason_str = str(reason).strip()

        # 1. Employer Candidate Limit
        if re.search(r"\b(employer\s+limit|employer\s+candidate\s+limit|candidate\s+application\s+limit)\b", reason_str, re.IGNORECASE) or reason_str.startswith("EMPLOYER_LIMIT:"):
            date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", reason_str)
            if date_match:
                return f"EMPLOYER_LIMIT: locked until {date_match.group(1)}"

            if company:
                try:
                    from app.services.company_cap import company_cap_tracker
                    lockout = company_cap_tracker.get_employer_lockout(company)
                    if lockout:
                        date_str = lockout[0].strftime("%Y-%m-%d")
                        return f"EMPLOYER_LIMIT: locked until {date_str}"
                except Exception:
                    pass

            days_match = re.search(r"locked for ~?(\d+)\s+more days", reason_str, re.IGNORECASE)
            if days_match:
                days = int(days_match.group(1))
                lock_date = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
                return f"EMPLOYER_LIMIT: locked until {lock_date}"

            clean = re.sub(r"^EMPLOYER_LIMIT:\s*", "", reason_str, flags=re.IGNORECASE).strip()
            return f"EMPLOYER_LIMIT: {clean}"

        # 2. Unsupported ATS / Portal
        if re.search(r"\bunsupported\b", reason_str, re.IGNORECASE) or reason_str.startswith("UNSUPPORTED:"):
            clean = re.sub(r"^UNSUPPORTED:\s*", "", reason_str, flags=re.IGNORECASE).strip()
            return f"UNSUPPORTED: {clean}"

        # 3. Company Cap / Cooldown
        if re.search(r"\b(per-company cap|company cap|cooldown)\b", reason_str, re.IGNORECASE) or reason_str.startswith("COMPANY_CAP:"):
            clean = re.sub(r"^COMPANY_CAP:\s*", "", reason_str, flags=re.IGNORECASE).strip()
            return f"COMPANY_CAP: {clean}"

        # 4. Seniority Exceeded
        if reason_str.startswith("SENIORITY_EXCEEDED:"):
            return reason_str
        if re.search(r"\bseniority\b", reason_str, re.IGNORECASE):
            clean = re.sub(r"^SENIORITY_EXCEEDED:\s*", "", reason_str).strip()
            return f"SENIORITY_EXCEEDED: {clean}"

        # 5. Experience Exceeded
        if reason_str.startswith("EXPERIENCE_EXCEEDED:"):
            return reason_str

        # 6. Wrong Function / Forbidden Domain
        if reason_str.startswith("WRONG_FUNCTION:"):
            return reason_str
        if re.search(r"\b(wrong_function|forbidden_domain|non-target domain)\b", reason_str, re.IGNORECASE):
            clean = re.sub(r"^WRONG_FUNCTION:\s*", "", reason_str).strip()
            return f"WRONG_FUNCTION: {clean}"

        # 7. Relevance Gate Near Miss / Low
        if re.search(r"\b(relevance\s+score|relevance_too_low|low relevance)\b", reason_str, re.IGNORECASE):
            clean = re.sub(r"^RELEVANCE_TOO_LOW:\s*", "", reason_str, flags=re.IGNORECASE).strip()
            return f"RELEVANCE_TOO_LOW: {clean}"

        # 8. Validation Failed
        if re.search(r"\bvalidation\b", reason_str, re.IGNORECASE):
            clean = re.sub(r"^(VALIDATION_FAILED:\s*|Validation blocked before submit:\s*)", "", reason_str, flags=re.IGNORECASE).strip()
            return f"VALIDATION_FAILED: {clean}"

        # 9. Overlay Blocked
        if re.search(r"\boverlay\b", reason_str, re.IGNORECASE):
            clean = re.sub(r"^(OVERLAY_BLOCKED:\s*|Submit blocked by un-dismissible overlay:\s*)", "", reason_str, flags=re.IGNORECASE).strip()
            return f"OVERLAY_BLOCKED: {clean}"

        # 10. Challenge Blocked
        if re.search(r"\b(challenge_blocked|security challenge|captcha)\b", reason_str, re.IGNORECASE):
            clean = re.sub(r"^(CHALLENGE_BLOCKED:\s*|Security challenge detected:\s*)", "", reason_str, flags=re.IGNORECASE).strip()
            return f"CHALLENGE_BLOCKED: {clean}"

        # 11. Watchdog Timeout
        if re.search(r"\btimeout\b", reason_str, re.IGNORECASE):
            clean = re.sub(r"^(TIMEOUT:\s*)", "", reason_str, flags=re.IGNORECASE).strip()
            return f"TIMEOUT: {clean}"

        # 12. If already categorized: ^[A-Z0-9_]{3,}:\s*(.+)$
        if re.match(r"^[A-Z0-9_]{3,}:\s*.+", reason_str):
            return reason_str

        # Fallback to single template: FILTERED: <reason>
        return f"FILTERED: {reason_str}"

    def set_diagnostics_report(self, report: str):
        """Store the pre-run diagnostics checklist to print verbatim at top of digest."""
        self._latest_diagnostics_report = report

    def _init_log(self):
        os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
        if not os.path.exists(self.log_path):
            with open(self.log_path, "w", encoding="utf-8") as f:
                f.write("# AutoApply Submissions Audit Log\n")
                f.write("# Format: ISO_TIMESTAMP | STATUS | COMPANY | ROLE | URL | SCORE | MATCH_REASON\n")

    def log_submission(
        self,
        company: str,
        role_title: str,
        job_url: str,
        match_reason: str = "Title/Keyword Match",
        match_score: float = 1.0,
        status: str = "submitted",
        skip_reason: Optional[str] = None,
        portal: Optional[str] = None,
        challenge_type: Optional[str] = None,
        stage_detected: Optional[str] = None,
        screenshot_path: Optional[str] = None,
        receipt_msg_id: Optional[Union[int, str]] = None,
        terminal_status: Optional[str] = None,
        photo_status: Optional[str] = None,
        job_id: Optional[str] = None
    ):
        """Append a submission, skip, or security challenge event to submissions.log and record in current run and outcome ledger."""
        ts = datetime.now(timezone.utc).isoformat()
        status_norm = status.lower()
        is_pending = (photo_status == "photo_pending" or receipt_msg_id == "photo_pending")
        p_status = "photo_pending" if is_pending else (photo_status or ("delivered" if receipt_msg_id else None))
        is_challenge = (
            status_norm in ("security_challenge", "captcha_detected", "challenge_blocked")
            or bool(challenge_type)
            or "security challenge" in (skip_reason or "").lower()
            or "captcha" in (skip_reason or "").lower()
        )

        if status_norm == "challenge_blocked":
            log_status = "CHALLENGE_BLOCKED"
            chal_label = challenge_type or "Security Challenge"
            stage_label = stage_detected or "detection"
            portal_label = portal or "direct"
            c_desc = f"[{chal_label} | stage={stage_label} | portal={portal_label}]"
            reason_text = f"{c_desc} {skip_reason or 'CHALLENGE_BLOCKED: Security challenge detected'}"
            if screenshot_path:
                reason_text += f" | evidence: {screenshot_path}"
        elif is_challenge:
            log_status = "SECURITY_CHALLENGE"
            chal_label = challenge_type or "Security Challenge"
            stage_label = stage_detected or "detection"
            portal_label = portal or "direct"
            c_desc = f"[{chal_label} | stage={stage_label} | portal={portal_label}]"
            reason_text = f"{c_desc} {skip_reason or 'Security challenge detected'}"
            if screenshot_path:
                reason_text += f" | evidence: {screenshot_path}"
        elif status_norm == "submitted":
            log_status = terminal_status or "SUBMITTED"
            reason_text = match_reason
        elif status_norm == "unverified":
            log_status = "UNVERIFIED"
            reason_text = skip_reason or match_reason
        elif status_norm in ("failed", "error"):
            log_status = "FAILED"
            reason_text = skip_reason or match_reason
        elif status_norm == "stalled":
            log_status = "STALLED"
            reason_text = skip_reason or "Launch exceeded 30m without terminal outcome"
        else:
            log_status = "SKIPPED"
            if skip_reason:
                skip_reason = self.unify_skip_reason(skip_reason, company=company)
            reason_text = skip_reason or match_reason

        line = f"{ts} | {log_status} | {company} | {role_title} | {job_url} | {match_score:.2f} | {reason_text}\n"

        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            logger.error(f"Failed writing to submissions.log: {e}")

        item = {
            "company": company,
            "role": role_title,
            "url": job_url,
            "score": match_score,
            "match_reason": match_reason,
            "skip_reason": skip_reason,
            "portal": portal,
            "challenge_type": challenge_type,
            "stage_detected": stage_detected,
            "screenshot_path": screenshot_path,
            "receipt_msg_id": receipt_msg_id,
            "photo_status": p_status,
            "photo_pending": is_pending,
            "terminal_status": log_status,
            "timestamp": ts,
        }

        # Synchronize with OutcomeLedger
        try:
            from app.services.intelligence_loop import outcome_ledger
            ledger_outcome = "challenge_blocked" if is_challenge else ("applied" if status_norm == "submitted" else status_norm)
            outcome_ledger.record_outcome(
                company=company,
                role=role_title,
                title=role_title,
                portal=portal or "direct",
                ats=portal or "direct",
                url=job_url,
                outcome=ledger_outcome,
                reason=skip_reason or reason_text,
                halt_reason=skip_reason or reason_text,
                stage=stage_detected,
                screenshot_path=screenshot_path,
                receipt_msg_id=receipt_msg_id,
                terminal_status=log_status,
                photo_status=p_status,
                photo_pending=is_pending,
                job_id=job_id
            )
        except Exception as oe:
            logger.debug(f"OutcomeLedger sync notice: {oe}")

        if status_norm == "submitted":
            self._total_submitted_count += 1
            if len(self._current_run_submitted) >= 100:
                self._current_run_submitted.pop(0)
            self._current_run_submitted.append(item)
        elif is_challenge:
            self._total_challenges_count += 1
            if len(self._current_run_challenges) >= 100:
                self._current_run_challenges.pop(0)
            self._current_run_challenges.append(item)
        else:
            self._total_skipped_count += 1
            if len(self._current_run_skipped) >= 100:
                self._current_run_skipped.pop(0)
            self._current_run_skipped.append(item)

    def generate_minimal_digest(self, window_minutes: Optional[int] = 1440) -> str:
        """
        Generate a minimal digest (Part C):
        Submitted applications (with receipts) + one-line totals (launched, blocked, skipped).
        Full per-role detail moves out of default view into verbose mode (/digest verbose).
        """
        launches = []
        try:
            from app.services.launch_tracker import launch_tracker
            launches = launch_tracker.get_last_24h_launches()
        except Exception as e:
            logger.debug(f"Error querying launch_tracker for minimal digest: {e}")

        # Submitted applications in window (from launch_tracker and current run)
        submitted_entries = []
        for lnch in launches:
            if lnch.get("status") == "SUBMITTED":
                submitted_entries.append({
                    "company": lnch.get("company", "Unknown"),
                    "role": lnch.get("role", "Target Role"),
                    "receipt": lnch.get("receipt_msg_id") or "None"
                })
        for item in self._current_run_submitted:
            comp = item.get("company", "Unknown")
            role = item.get("role", "Target Role")
            if not any(s["company"] == comp and s["role"] == role for s in submitted_entries):
                submitted_entries.append({
                    "company": comp,
                    "role": role,
                    "receipt": item.get("receipt_msg_id") or "Recorded"
                })

        launched_count = len(launches) if launches else len(self._current_run_submitted)
        blocked_count = sum(1 for l in launches if l.get("status") in ("CHALLENGE_BLOCKED", "BLOCKED"))
        if len(self._current_run_challenges) > blocked_count:
            blocked_count = len(self._current_run_challenges)
        skipped_count = sum(1 for l in launches if l.get("status") == "SKIPPED")
        if len(self._current_run_skipped) > skipped_count:
            skipped_count = len(self._current_run_skipped)

        lines = [
            "📊 *AutoApply Run Digest*",
            "",
            "• *Submitted Applications:*",
        ]
        if submitted_entries:
            for sub in submitted_entries:
                lines.append(f"  • *{sub['company']}* — {sub['role']} | Receipt: `{sub['receipt']}`")
        else:
            lines.append("  • None in last 24h")

        lines.append("")
        lines.append(f"• *Totals (Last 24h):* Launched: {launched_count} | Blocked: {blocked_count} | Skipped: {skipped_count}")
        rss_line = get_daemon_rss_telemetry_line()
        if rss_line:
            lines.append(rss_line)
        return "\n".join(lines)

    def generate_run_digest(self, window_minutes: Optional[int] = None, minimal: bool = False) -> str:
        """Generate a formatted Markdown digest of the run for Telegram and CLI."""
        if minimal:
            return self.generate_minimal_digest(window_minutes=window_minutes)

        # Collect verified submitted entries across launch_tracker and current run
        submitted_entries = []
        is_custom_log = (self.log_path != SUBMISSIONS_LOG_PATH)
        if not is_custom_log:
            try:
                from app.services.launch_tracker import launch_tracker
                launches = launch_tracker.get_last_24h_launches()
                if launches:
                    for lnch in launches:
                        if lnch.get("status") == "SUBMITTED":
                            submitted_entries.append({
                                "company": lnch.get("company", "Unknown"),
                                "role": lnch.get("role", "Target Role"),
                                "match_reason": lnch.get("reason", "Autonomous Scout"),
                                "score": lnch.get("fit_score", 1.0) or 1.0
                            })
            except Exception:
                pass

        for item in self._current_run_submitted:
            comp = item.get("company", "Unknown")
            role = item.get("role", "Target Role")
            if not any(s["company"] == comp and s["role"] == role for s in submitted_entries):
                submitted_entries.append(item)

        total_sub = max(len(submitted_entries), len(self._current_run_submitted), self._total_submitted_count)
        total_challenges = max(len(self._current_run_challenges), self._total_challenges_count)
        total_skip = max(len(self._current_run_skipped), self._total_skipped_count) + total_challenges

        lines = []

        # Print verbatim diagnostics checklist at top of digest if present
        if self._latest_diagnostics_report:
            lines.append(f"```\n{self._latest_diagnostics_report}\n```")
            lines.append("")

        lines.extend([
            "📊 *AutoApply Run Digest Summary*",
            f"• *Submitted Applications:* {total_sub}",
            f"• *Skipped / Blocked Roles:* {total_skip}",
            f"• *Challenge-Blocked Roles:* {total_challenges}",
            ""
        ])
        rss_line = get_daemon_rss_telemetry_line()
        if rss_line:
            lines.append(rss_line)
            lines.append("")

        # Registry & Coverage Digest Line (A.6)
        try:
            from app.services.company_registry import format_registry_digest_line
            lines.append(format_registry_digest_line())
            lines.append("ℹ️ _Definition: 'Launched' counts pipeline attempt dispatches today; 'Submitted Applications' counts verified terminal submissions._")
            lines.append("")
        except Exception as ce:
            logger.debug(f"Coverage line inclusion notice: {ce}")

        # Pipeline Funnel Breakdown (ALWAYS included, even when eligible = 0)
        try:
            from app.services.job_scraper import get_current_funnel
            funnel = get_current_funnel()
            lines.append("🔻 *Pipeline Funnel Breakdown:*")
            lines.append(f"```\n{funnel.to_table()}\n```")
            lines.append("")
        except Exception as fe:
            logger.debug(f"Funnel digest inclusion notice: {fe}")

        # Outcome Ledger Section (FIX 1, FIX 2, FIX 3)
        try:
            from app.services.launch_tracker import launch_tracker
            launches = launch_tracker.get_last_24h_launches()
            lines.append("📋 *Outcome Ledger (Last 24h Launches & Outcomes):*")
            if launches:
                for lnch in launches[-20:]:
                    status = lnch.get("status", "UNKNOWN")
                    comp = lnch.get("company", "Unknown")
                    role = lnch.get("role", "Role")
                    ts = lnch.get("timestamp", "")[:19].replace("T", " ")
                    shot = lnch.get("screenshot_path") or "None"
                    shot_disp = f"📸 {os.path.basename(shot)}" if shot != "None" else "📸 None"
                    receipt = lnch.get("receipt_msg_id") or "None"
                    reason = lnch.get("reason")
                    note = f" — _{reason}_" if reason and status != "SUBMITTED" else ""
                    lines.append(f"• `[{status}]` *{comp}* — {role} | {ts} | {shot_disp} | Receipt: {receipt}{note}")
            else:
                lines.append("• _No application launches recorded in the past 24 hours._")
            lines.append("")
        except Exception as le:
            logger.debug(f"Outcome Ledger section in digest notice: {le}")

        if submitted_entries:
            lines.append("✅ *Successfully Submitted:*")
            for item in submitted_entries:
                score_str = f" (Score: {item['score']:.2f})" if isinstance(item.get("score"), (int, float)) else ""
                trigger = item.get("match_reason") or "Autonomous Scout"
                lines.append(f"• *{item['company']}* — {item['role']}")
                lines.append(f"  ↳ Trigger: _{trigger}_{score_str}")
            lines.append("")

        if self._current_run_challenges:
            lines.append("⚠️ *Manual-only: security challenge:*")
            for item in self._current_run_challenges:
                c_type = item.get("challenge_type") or "Security Challenge"
                stage = item.get("stage_detected") or "detection"
                shot_info = f" [evidence: {item['screenshot_path']}]" if item.get("screenshot_path") else ""
                lines.append(f"• *{item['company']}* — {item['role']} ({c_type}, {stage}){shot_info}")
                lines.append(f"  ↳ Direct URL: {item['url']}")
            lines.append("")

        if self._current_run_skipped:
            lines.append("⏭ *Skipped / Filtered:*")
            seen_companies = set()
            try:
                from app.services.company_cap import normalize_company_name
            except Exception:
                normalize_company_name = None

            for item in self._current_run_skipped:
                company = item.get("company", "Unknown")
                norm_c = normalize_company_name(company) if normalize_company_name else company.strip().lower()
                if not norm_c:
                    norm_c = company.strip().lower()

                if norm_c in seen_companies:
                    continue
                seen_companies.add(norm_c)

                role = item.get("role") or "All Roles"
                raw_reason = item.get("skip_reason") or "Filtered"
                reason = self.unify_skip_reason(raw_reason, company=company)
                lines.append(f"• *{company}* — {role}")
                lines.append(f"  ↳ Reason: {reason}")
            lines.append("")

        # Filter Breakdown: last-24h filtered roles grouped by terminal gate code
        if self._current_run_skipped or self._current_run_challenges:
            from collections import Counter
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            gate_counts: Counter = Counter()
            for item in self._current_run_skipped:
                raw = item.get("skip_reason") or item.get("match_reason") or "FILTERED"
                unified = self.unify_skip_reason(raw, company=item.get("company"))
                # Extract gate code (part before first colon or first whitespace)
                m = re.match(r"^([A-Z][A-Z_0-9]+)", unified)
                gate_code = m.group(1) if m else "FILTERED"
                # Retire H-1B / sponsorship reason codes per user direction 2026-09-25
                if gate_code in ("H1B_UNSUPPORTED", "SPONSORSHIP_UNSUPPORTED", "H1B", "SPONSORSHIP"):
                    continue
                gate_counts[gate_code] += 1
            for item in self._current_run_challenges:
                gate_counts["CHALLENGE_BLOCKED"] += 1
            if gate_counts:
                lines.append("📊 *Filter Breakdown (Last 24h):*")
                total_filtered = sum(gate_counts.values())
                for code, cnt in gate_counts.most_common():
                    pct = round(100 * cnt / total_filtered) if total_filtered else 0
                    lines.append(f"  • `{code}`: {cnt} ({pct}%)")
                lines.append(f"  _Total filtered/skipped: {total_filtered}_")
                lines.append("")

        # Halted Breakdown: Last 24h halted roles grouped by reason code (Rule 12)
        try:
            from app.services.intelligence_loop import outcome_ledger
            outcomes = outcome_ledger.get_last_24h_outcomes(hours=24)
            halt_counts = Counter()
            for oc in outcomes:
                outc = (oc.get("outcome") or "").lower()
                if outc in ("challenge_blocked", "failed", "stalled", "skipped") or "halt" in outc:
                    h_code = oc.get("halt_reason") or oc.get("reason") or outc.upper()
                    m = re.match(r"^([A-Z][A-Z_0-9]+)", str(h_code).strip())
                    code = m.group(1) if m else outc.upper()
                    halt_counts[code] += 1
            if halt_counts:
                lines.append("🛑 *Halted Roles Breakdown (Last 24h):*")
                total_halted = sum(halt_counts.values())
                for code, cnt in halt_counts.most_common():
                    lines.append(f"  • `{code}`: {cnt}")
                lines.append(f"  _Total halted/blocked: {total_halted}_")
                lines.append("")
        except Exception:
            pass

        # Live Digest Findability & Audit Access (FIX 3)
        lines.append("📍 *Live Digest & Audit Access:*")
        lines.append("• Telegram Command: `/digest` (shows 24h outcome ledger, receipts & screenshots)")
        lines.append("• CLI View: `python3 -m app.services.audit_logger` (from `sid_job_scrip/`)")
        lines.append(f"• Submissions Log: `{self.log_path}`")
        try:
            from app.services.launch_tracker import LAUNCH_TRACKER_PATH
            lines.append(f"• Launch Tracker: `{LAUNCH_TRACKER_PATH}`")
        except Exception:
            pass
        lines.append(f"• Outcome Ledger: `{os.path.join(settings.DATA_DIR, 'outcome_ledger.json')}`")
        return "\n".join(lines)

    def reset_run(self):
        """Clear memory for a new scan run."""
        self._current_run_submitted.clear()
        self._current_run_skipped.clear()
        self._current_run_challenges.clear()
        self._total_submitted_count = 0
        self._total_skipped_count = 0
        self._total_challenges_count = 0
        self._latest_diagnostics_report = None


def collect_digest_evidence_photos(launches: Optional[List[Dict[str, Any]]] = None) -> Tuple[List[Dict[str, Any]], int, Optional[str]]:
    """
    Given launch records (or querying launch_tracker for last 24h if None):
    1. Extracts valid screenshots on disk per outcome-ledger entry.
    2. Formats caption: 'Company — Role | timestamp | outcome'.
    3. Missing files are skipped silently (no errors).
    4. Caps at the 10 most recent screenshots.
    5. If > 10, calculates overflow count N and returns overflow line:
       '+N more on disk: ~/Desktop/screenshots/'.

    Returns:
        (photos_to_attach, overflow_count, overflow_line)
    """
    if launches is None:
        try:
            from app.services.launch_tracker import launch_tracker
            launches = list(launch_tracker.get_last_24h_launches())
        except Exception:
            launches = []
        try:
            from app.services.intelligence_loop import outcome_ledger
            outcomes = outcome_ledger.get_last_24h_outcomes()
            seen_shots = {l.get("screenshot_path") for l in launches if l.get("screenshot_path")}
            for o in outcomes:
                ss = o.get("evidence_screenshot") or o.get("screenshot_path")
                if ss and ss not in seen_shots:
                    launches.append({
                        "company": o.get("company", "Unknown"),
                        "role": o.get("role", "Role"),
                        "screenshot_path": ss,
                        "timestamp": o.get("timestamp"),
                        "status": o.get("terminal_status") or o.get("outcome") or "SUBMITTED",
                        "photo_status": o.get("photo_status"),
                        "receipt_msg_id": o.get("receipt_msg_id")
                    })
        except Exception:
            pass

    valid_photos = []
    for lnch in (launches or []):
        shot_path = lnch.get("screenshot_path") or lnch.get("evidence_screenshot")
        if not shot_path or not isinstance(shot_path, str) or shot_path.strip().lower() == "none":
            continue
        exp_path = os.path.expanduser(shot_path.strip())
        try:
            if os.path.isfile(exp_path) and os.path.getsize(exp_path) > 0:
                comp = lnch.get("company", "Unknown")
                role = lnch.get("role", "Role")
                raw_ts = lnch.get("timestamp") or ""
                ts = raw_ts[:19].replace("T", " ") if raw_ts else ""
                outcome = lnch.get("status") or lnch.get("terminal_outcome") or "UNKNOWN"
                caption = f"{comp} — {role} | {ts} | {outcome}" if ts else f"{comp} — {role} | {outcome}"
                valid_photos.append({
                    "path": exp_path,
                    "caption": caption,
                    "company": comp,
                    "role": role,
                    "timestamp": raw_ts,
                    "outcome": outcome,
                })
        except Exception:
            continue

    total_valid = len(valid_photos)
    if total_valid > 10:
        overflow_count = total_valid - 10
        photos_to_attach = valid_photos[-10:]
        overflow_line = f"+{overflow_count} more on disk: ~/Desktop/screenshots/"
    else:
        overflow_count = 0
        photos_to_attach = valid_photos
        overflow_line = None

    return photos_to_attach, overflow_count, overflow_line


audit_logger = AuditLogger()


if __name__ == "__main__":
    print(audit_logger.generate_run_digest())
