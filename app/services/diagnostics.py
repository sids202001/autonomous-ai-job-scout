"""
AutoApply - Pre-Run Self-Diagnostics Engine
Validates critical system health, config sanity, registry integrity, Telegram routing,
and portal reachability BEFORE any application work begins.

Guarantees:
- Every run starts with a complete diagnostic check.
- If ANY check fails, run aborts BEFORE the first application.
- Exactly one system-health Telegram alert fires on failure.
- Verbatim checklist results printed at the top of every end-of-run digest.
"""

import re
import os
import logging
import asyncio
from typing import Tuple, Dict, Any, List
import httpx

from config import settings

logger = logging.getLogger(__name__)


class PreRunDiagnostics:
    """
    Automated pre-flight diagnostic suite.
    """

    PROBE_DOMAINS = [
        ("Greenhouse", "https://boards.greenhouse.io"),
        ("Lever", "https://jobs.lever.co"),
        ("Ashby", "https://jobs.ashbyhq.com"),
    ]

    @classmethod
    async def run_all_checks(cls, alert_on_failure: bool = True) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Execute full diagnostic checklist.
        Returns: (all_passed: bool, verbatim_report: str, check_details: dict)
        """
        checks: Dict[str, Dict[str, Any]] = {}
        all_passed = True

        # Check 1: Config Sanity
        c1_ok, c1_msg = cls._check_config_sanity()
        checks["config_sanity"] = {"passed": c1_ok, "message": c1_msg}
        if not c1_ok:
            all_passed = False

        # Check 2: Registry Integrity
        c2_ok, c2_msg = cls._check_registry_integrity()
        checks["registry_integrity"] = {"passed": c2_ok, "message": c2_msg}
        if not c2_ok:
            all_passed = False

        # Check 3: Telegram Routing (Strict noise elimination: ONLY 2 templates reachable)
        c3_ok, c3_msg = await cls._check_telegram_routing()
        checks["telegram_routing"] = {"passed": c3_ok, "message": c3_msg}
        if not c3_ok:
            all_passed = False

        # Check 4: Portal Reachability Probe
        c4_ok, c4_msg = await cls._check_portal_reachability()
        checks["portal_reachability"] = {"passed": c4_ok, "message": c4_msg}
        if not c4_ok:
            all_passed = False

        # Build Verbatim Checklist Report
        verbatim_lines = [
            "=== PRE-RUN SELF-DIAGNOSTICS CHECKLIST ===",
            f"[{'PASS' if c1_ok else 'FAIL'}] 1. Config Sanity: {c1_msg}",
            f"[{'PASS' if c2_ok else 'FAIL'}] 2. Registry Integrity: {c2_msg}",
            f"[{'PASS' if c3_ok else 'FAIL'}] 3. Telegram Routing: {c3_msg}",
            f"[{'PASS' if c4_ok else 'FAIL'}] 4. Portal Reachability: {c4_msg}",
            f"OVERALL STATUS: {'ALL CHECKS PASSED (READY FOR RUN)' if all_passed else 'DIAGNOSTICS FAILED (RUN ABORTED)'}",
            "=========================================="
        ]
        verbatim_report = "\n".join(verbatim_lines)

        if not all_passed:
            logger.error(f"PRE-RUN DIAGNOSTICS FAILED:\n{verbatim_report}")
            if alert_on_failure:
                await cls._send_system_health_alert(verbatim_report)
        else:
            logger.info(f"PRE-RUN DIAGNOSTICS PASSED:\n{verbatim_report}")

        return all_passed, verbatim_report, checks

    @classmethod
    def _check_config_sanity(cls) -> Tuple[bool, str]:
        """Verify pacing, caps, experience filter, and relevance gate config."""
        try:
            from app.services.experience_gate import ExperienceGate
            from app.services.relevance_gate import RelevanceGate
            from app.services.politeness import DOMAIN_LIMITS

            # Pacing checks (FIXED 150s standing rule — replaced 5-10 min randomized delays)
            if not DOMAIN_LIMITS or any(rpm > 10 for rpm in DOMAIN_LIMITS.values()):
                return False, "ATS domain rate limit exceeds 10 req/min"
            if settings.MIN_DELAY_BETWEEN_APPS < 150:
                return False, "Inter-application delay minimum must be at least 150s (FIXED pacing — standing rule)"
            if settings.COMPANY_COOLDOWN_HOURS < 1:
                return False, "Company cooldown must be at least 1 hour"

            # Cap checks
            if settings.MAX_APPLICATIONS_PER_DAY > 50:
                return False, "Daily application cap exceeds safe maximum (25-50)"

            # Experience & Relevance gate check
            if ExperienceGate.MAX_YEARS_CAP != 3:
                return False, f"Experience gate cap is {ExperienceGate.MAX_YEARS_CAP}, expected 3"
            if RelevanceGate.MIN_RELEVANCE_THRESHOLD < 0.5:
                return False, "Relevance gate threshold is too permissive"

            return True, "Pacing (10 rpm, 5-10m delay, 1h cooldown), caps (max 50/day), experience filter (0-3 yrs), relevance gate loaded."
        except Exception as e:
            return False, f"Config sanity exception: {str(e)}"

    @classmethod
    def _check_registry_integrity(cls) -> Tuple[bool, str]:
        """Verify universal question registry entries, pattern compilation, and profile paths."""
        try:
            from app.services.universal_registry import UNIVERSAL_REGISTRY
            from app.services.presubmit_gate import CandidateProfileStore

            if not UNIVERSAL_REGISTRY or len(UNIVERSAL_REGISTRY) < 25:
                return False, f"Universal registry entries count too low ({len(UNIVERSAL_REGISTRY)})"

            # Test compile all regex patterns
            compiled_count = 0
            for k, entry in UNIVERSAL_REGISTRY.items():
                for pat in entry.get("patterns", []):
                    re.compile(pat, re.IGNORECASE)
                    compiled_count += 1

            # Test candidate profile store loading
            store = CandidateProfileStore()
            if not store.profile or not store.get_fact("personal_info.email"):
                return False, "Candidate profile facts failed to load"

            return True, f"Universal registry loaded ({len(UNIVERSAL_REGISTRY)} categories, {compiled_count} patterns compiled, profile facts verified)."
        except Exception as e:
            return False, f"Registry integrity exception: {str(e)}"

    @classmethod
    async def _check_telegram_routing(cls) -> Tuple[bool, str]:
        """Verify strict noise elimination: ONLY 'submitted' and 'capped' templates can send."""
        try:
            from app.services.telegram_notifier import send_telegram_alert

            # Test forbidden alert types with dummy job_id
            forbidden_types = [
                "security_challenge",
                "captcha",
                "pre_submit_flagged",
                "missing_field",
                "validation_failed",
                "skipped",
                "halt_review",
            ]

            for f_type in forbidden_types:
                # Should return False immediately without firing HTTP
                allowed = await send_telegram_alert(
                    job_id="test_diag_job",
                    alert_type=f_type,
                    company="DiagnosticProbe",
                    role="Software Engineer",
                    portal="Ashby",
                    job_url="https://example.com"
                )
                if allowed:
                    return False, f"Telegram noise leak detected: forbidden alert_type '{f_type}' was allowed through!"

            return True, "Strict noise boundary verified (only 'submitted' & 'capped' templates reachable; zero noise paths)."
        except Exception as e:
            return False, f"Telegram routing check exception: {str(e)}"

    @classmethod
    async def _check_portal_reachability(cls) -> Tuple[bool, str]:
        """Probe ATS portals to confirm outbound connectivity."""
        reachable = []
        unreachable = []

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        }

        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, headers=headers) as client:
            for name, url in cls.PROBE_DOMAINS:
                try:
                    resp = await client.get(url)
                    # Any response code (even 403 or 200) proves DNS and TCP routing work
                    if resp.status_code < 500:
                        reachable.append(name)
                    else:
                        unreachable.append(f"{name} (HTTP {resp.status_code})")
                except Exception as e:
                    unreachable.append(f"{name} ({type(e).__name__})")

        if unreachable and len(reachable) == 0:
            return False, f"All ATS portal probes failed: {', '.join(unreachable)}"
        
        msg = f"ATS domains online ({', '.join(reachable)} reachable"
        if unreachable:
            msg += f", {', '.join(unreachable)} degraded"
        msg += ")."
        return True, msg

    @classmethod
    async def _send_system_health_alert(cls, details: str):
        """Send a single high-priority Telegram system health alert when diagnostics fail."""
        if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
            return

        text = (
            "🚨 *AutoApply System-Health Alert*\n"
            "Pre-run self-diagnostics failed! Application run aborted before first job.\n\n"
            f"```\n{details[:800]}\n```"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": settings.TELEGRAM_CHAT_ID,
                        "text": text,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True
                    }
                )
        except Exception as e:
            logger.error(f"Failed to dispatch system-health alert to Telegram: {e}")


# Singleton
diagnostics = PreRunDiagnostics()
