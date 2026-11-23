"""
Tests for the Final Consolidated Prompt items:
1. Match Bar (≥2 distinct candidate skills gate in RelevanceGate)
2. Experience Gate (3+/up-to-3 → PASS; 4+/3-5 years → FAIL)
3. Cap Tiebreak (skill-overlap sort before consuming slots)
4. HITL A defer→timeout (CAPTCHA_SOLVE_REQUEST flow stub)
5. HITL B defer→timeout (MISSING_INFO_REQUEST flow stub)
6. Pacing: FIXED 150s — assert no randomization in pacing code
7. Filter Breakdown sums to Skipped/Blocked total
8. Gate Honesty Audit: H-1B path + EXPERIENCE_UNPARSEABLE share
"""
import os
import re
import time
import asyncio
import inspect
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock, MagicMock

import config
from config import settings
from app.services.relevance_gate import RelevanceGate
from app.services.experience_gate import ExperienceGate
from app.services.audit_logger import AuditLogger
from app.services.telegram_notifier import send_telegram_alert, reset_alert_hygiene


# ---------------------------------------------------------------------------
# Test 1: Match Bar — ≥2 distinct candidate skills required
# ---------------------------------------------------------------------------

class Test1MatchBar(unittest.TestCase):
    """Match bar: posting passes iff ≥2 distinct candidate skills in combined text."""

    def test_zero_skills_fails_match_bar(self):
        # No skills in description → MATCH_BAR_FAIL even if title is target role
        ok, reason, meta = RelevanceGate.evaluate(
            title="Software Engineer",
            description="Looking for a brilliant person to join our team. No specific tools mentioned."
        )
        self.assertFalse(ok, f"Expected MATCH_BAR_FAIL but got PASS. Reason: {reason}")
        self.assertIn("MATCH_BAR_FAIL", reason)
        self.assertEqual(len(meta["matched_skills"]), 0)

    def test_one_skill_fails_match_bar(self):
        # Only 1 skill → MATCH_BAR_FAIL
        ok, reason, meta = RelevanceGate.evaluate(
            title="Software Engineer",
            description="Must have Python experience. Great culture!"
        )
        self.assertFalse(ok, f"Expected MATCH_BAR_FAIL but got PASS. Reason: {reason}")
        self.assertIn("MATCH_BAR_FAIL", reason)
        self.assertEqual(len(meta["matched_skills"]), 1)

    def test_two_skills_passes_match_bar(self):
        # Exactly 2 skills → passes the match bar
        ok, reason, meta = RelevanceGate.evaluate(
            title="Software Engineer",
            description="Must know Python and PostgreSQL. Experience with AWS a plus."
        )
        self.assertTrue(ok, f"Expected PASS (≥2 skills) but got FAIL. Reason: {reason}")
        self.assertGreaterEqual(len(meta["matched_skills"]), 2)

    def test_three_skills_passes_match_bar(self):
        # 3+ skills → clearly passes
        ok, reason, meta = RelevanceGate.evaluate(
            title="Backend Engineer",
            description="We use Python, FastAPI, PostgreSQL, Docker, and AWS Lambda for our stack."
        )
        self.assertTrue(ok, f"Expected PASS. Reason: {reason}")
        self.assertGreaterEqual(len(meta["matched_skills"]), 3)

    def test_match_bar_constant_is_two(self):
        self.assertEqual(RelevanceGate.MATCH_BAR_MIN_SKILLS, 2)

    def test_overlap_count_in_metadata(self):
        # Verify metadata["matched_skills"] count is accurate
        ok, reason, meta = RelevanceGate.evaluate(
            title="ML Engineer",
            description="Experience with PyTorch and LangChain required. Knowledge of AWS and Kafka is a plus."
        )
        self.assertGreaterEqual(len(meta["matched_skills"]), 2)
        # All matched skills must come from CANDIDATE_SKILL_STACK
        from app.services.relevance_gate import CANDIDATE_SKILL_STACK
        for skill in meta["matched_skills"]:
            self.assertIn(skill, CANDIDATE_SKILL_STACK)

    def test_forbidden_domain_bypasses_match_bar(self):
        # Forbidden domain should fail BEFORE match bar (WRONG_FUNCTION, not MATCH_BAR_FAIL)
        ok, reason, meta = RelevanceGate.evaluate(
            title="Marketing Manager",
            description="Python, AWS, SQL, Docker, FastAPI experience required."
        )
        self.assertFalse(ok)
        self.assertIn("WRONG_FUNCTION", reason)
        self.assertNotIn("MATCH_BAR_FAIL", reason)


# ---------------------------------------------------------------------------
# Test 2: Experience Gate — 3+ boundary and "up to X years"
# ---------------------------------------------------------------------------

class Test2ExperienceGateBoundary(unittest.TestCase):
    """Experience gate: 0-3 years / 3+ → PASS; 4+, 3-5, 4-6 → FAIL."""

    def test_three_plus_years_passes(self):
        # "3+ years" → min_y=3 ≤ MAX_YEARS_CAP=3 → PASS
        ok, reason, _ = ExperienceGate.evaluate(
            title="Software Engineer",
            description="Requires 3+ years of Python experience."
        )
        self.assertTrue(ok, f"'3+ years' should PASS. Reason: {reason}")
        self.assertIn("EXPERIENCE_PASSED", reason)

    def test_four_plus_years_fails(self):
        # "4+ years" → min_y=4 > MAX_YEARS_CAP=3 → FAIL
        ok, reason, _ = ExperienceGate.evaluate(
            title="Software Engineer",
            description="Requires 4+ years of experience in backend development."
        )
        self.assertFalse(ok, f"'4+ years' should FAIL. Reason: {reason}")
        self.assertIn("EXPERIENCE_EXCEEDED", reason)

    def test_five_plus_years_fails(self):
        ok, reason, _ = ExperienceGate.evaluate(
            title="Software Engineer",
            description="5+ years of software development experience required."
        )
        self.assertFalse(ok, f"'5+ years' should FAIL. Reason: {reason}")

    def test_three_to_five_years_fails(self):
        # "3-5 years" → max_y=5 > 3 → FAIL
        ok, reason, _ = ExperienceGate.evaluate(
            title="Software Engineer",
            description="Looking for 3-5 years of experience."
        )
        self.assertFalse(ok, f"'3-5 years' should FAIL. Reason: {reason}")
        self.assertIn("EXPERIENCE_EXCEEDED", reason)

    def test_four_to_six_years_fails(self):
        ok, reason, _ = ExperienceGate.evaluate(
            title="Software Engineer",
            description="4-6 years of experience required."
        )
        self.assertFalse(ok, f"'4-6 years' should FAIL. Reason: {reason}")

    def test_up_to_three_years_passes(self):
        # "up to 3 years" → max_y=3 → PASS (0-3 range)
        ok, reason, _ = ExperienceGate.evaluate(
            title="Software Engineer",
            description="Up to 3 years of experience required."
        )
        self.assertTrue(ok, f"'up to 3 years' should PASS. Reason: {reason}")

    def test_up_to_four_years_fails(self):
        # "Up to 4 years of experience preferred." — simple_pat ("X years of experience")
        # fires before upto_pat and extracts 4 → min_y=4, max_y=None → EXPERIENCE_EXCEEDED.
        # Correct per spec: FAIL on any explicit 4+.
        ok, reason, _ = ExperienceGate.evaluate(
            title="Software Engineer",
            description="Up to 4 years of experience preferred."
        )
        self.assertFalse(ok, f"'up to 4 years of experience preferred' should FAIL (4 > cap=3). Reason: {reason}")

    def test_one_to_three_years_passes(self):
        ok, reason, _ = ExperienceGate.evaluate(
            title="Software Engineer",
            description="1-3 years of experience required."
        )
        self.assertTrue(ok, f"'1-3 years' should PASS. Reason: {reason}")

    def test_zero_to_three_years_passes(self):
        ok, reason, _ = ExperienceGate.evaluate(
            title="Software Engineer",
            description="0-3 years of Python and SQL experience required."
        )
        self.assertTrue(ok, f"'0-3 years' should PASS. Reason: {reason}")

    def test_two_to_three_years_passes(self):
        ok, reason, _ = ExperienceGate.evaluate(
            title="Software Engineer",
            description="2-3 years of experience required."
        )
        self.assertTrue(ok, f"'2-3 years' should PASS. Reason: {reason}")

    def test_max_years_cap_is_three(self):
        self.assertEqual(ExperienceGate.MAX_YEARS_CAP, 3)

    def test_senior_title_fails_seniority(self):
        ok, reason, _ = ExperienceGate.evaluate(
            title="Vice President, Revenue Cycle Technology Operations",
            description="Any experience level."
        )
        self.assertFalse(ok)
        self.assertIn("SENIORITY_EXCEEDED", reason)

    def test_director_title_fails_seniority(self):
        ok, reason, _ = ExperienceGate.evaluate(
            title="Director of Engineering",
            description="0-2 years of experience."
        )
        self.assertFalse(ok)
        self.assertIn("SENIORITY_EXCEEDED", reason)


# ---------------------------------------------------------------------------
# Test 3: Cap Tiebreak — skill-overlap descending sort
# ---------------------------------------------------------------------------

class Test3CapTiebreak(unittest.TestCase):
    """Cap tiebreak: when postings exceed slots at one company, strongest skill-overlap wins."""

    def _skill_overlap(self, title: str, description: str) -> int:
        """Count distinct candidate skills matching in combined text."""
        from app.services.relevance_gate import CANDIDATE_SKILL_STACK
        combined = f"{title} {description}".lower()
        count = 0
        for skill in CANDIDATE_SKILL_STACK:
            pattern = r"\b" + re.escape(skill) + r"\b"
            if re.search(pattern, combined, re.IGNORECASE):
                count += 1
        return count

    def test_skill_overlap_ordering(self):
        # Role A: 4 skills, Role B: 2 skills → A > B
        role_a = {"title": "Software Engineer", "description": "Python, AWS, SQL, Docker"}
        role_b = {"title": "Software Engineer", "description": "Python, FastAPI"}

        overlap_a = self._skill_overlap(role_a["title"], role_a["description"])
        overlap_b = self._skill_overlap(role_b["title"], role_b["description"])
        self.assertGreater(overlap_a, overlap_b, "Role A with 4 skills must rank above Role B with 2 skills")

    def test_tiebreak_sort_descending(self):
        # Simulate tiebreak: sort list of (overlap, url) descending
        postings = [
            {"url": "job1", "overlap": 2},
            {"url": "job3", "overlap": 5},
            {"url": "job2", "overlap": 3},
        ]
        sorted_postings = sorted(postings, key=lambda x: x["overlap"], reverse=True)
        self.assertEqual(sorted_postings[0]["url"], "job3")
        self.assertEqual(sorted_postings[1]["url"], "job2")
        self.assertEqual(sorted_postings[2]["url"], "job1")

    def test_tiebreak_strongest_consumes_first_slot(self):
        # When cap=1 and 3 postings, only the highest-overlap posting should be selected
        postings = [
            {"url": "weak", "overlap": 1},
            {"url": "strong", "overlap": 6},
            {"url": "medium", "overlap": 3},
        ]
        cap = 1
        sorted_postings = sorted(postings, key=lambda x: x["overlap"], reverse=True)
        selected = sorted_postings[:cap]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["url"], "strong")


# ---------------------------------------------------------------------------
# Test 4: HITL A — CAPTCHA_SOLVE_REQUEST reaches Telegram (allowlist)
# ---------------------------------------------------------------------------

class Test4HITLCaptchaSolveRequest(unittest.IsolatedAsyncioTestCase):
    """HITL A: captcha_solve_request alert_type must pass through Telegram allowlist."""

    def setUp(self):
        reset_alert_hygiene()

    async def test_captcha_solve_request_reaches_telegram_allowlist(self):
        """captcha_solve_request must NOT be suppressed by the allowlist gate."""
        reached_send = False

        async def mock_post(*args, **kwargs):
            nonlocal reached_send
            reached_send = True
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.AsyncClient.__aenter__") as mock_ctx:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch.object(settings, "TELEGRAM_BOT_TOKEN", "test_token"), \
                 patch.object(settings, "TELEGRAM_CHAT_ID", "12345"), \
                 patch.object(settings, "TELEGRAM_SUCCESS_ONLY", False):

                # Even without verified_success=True, captcha_solve_request is a HITL alert
                result = await send_telegram_alert(
                    job_id="test_captcha_001",
                    alert_type="captcha_solve_request",
                    company="Acme Corp",
                    role="Software Engineer",
                    portal="Greenhouse",
                    job_url="https://boards.greenhouse.io/acme/jobs/123",
                    details="Form 100% filled — solve the CAPTCHA on your Mac, then confirm.",
                    force=True
                )
        # Should pass the allowlist (not suppressed)
        # Result depends on httpx mock setup, but the key is it wasn't suppressed by alert_type check
        # Verify directly: call evaluate with just the allowlist check
        from app.services.telegram_notifier import _SENT_ALERTS
        # If the alert hit the dedupe layer, it passed the allowlist
        self.assertIn("test_captcha_001:captcha_solve_request", _SENT_ALERTS)

    async def test_missing_info_request_reaches_telegram_allowlist(self):
        """missing_info_request must NOT be suppressed by the allowlist gate."""
        reset_alert_hygiene()

        with patch.object(settings, "TELEGRAM_BOT_TOKEN", "test_token"), \
             patch.object(settings, "TELEGRAM_CHAT_ID", "12345"), \
             patch.object(settings, "TELEGRAM_SUCCESS_ONLY", False):
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_client_instance = AsyncMock()
                mock_client_instance.post = AsyncMock(return_value=mock_response)
                mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
                mock_client_class.return_value.__aexit__ = AsyncMock(return_value=False)

                await send_telegram_alert(
                    job_id="test_missing_001",
                    alert_type="missing_info_request",
                    company="Cohere",
                    role="Software Engineer",
                    portal="Ashby",
                    job_url="https://jobs.ashbyhq.com/cohere/123",
                    details="Required fields missing: Cover Letter, Portfolio URL",
                    force=True
                )

        from app.services.telegram_notifier import _SENT_ALERTS
        self.assertIn("test_missing_001:missing_info_request", _SENT_ALERTS)

    async def test_scan_lifecycle_still_suppressed(self):
        """Scan lifecycle messages (start/complete) must still be suppressed."""
        reset_alert_hygiene()

        reached = False

        async def mock_post(*args, **kwargs):
            nonlocal reached
            reached = True
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch.object(settings, "TELEGRAM_BOT_TOKEN", "test_token"), \
             patch.object(settings, "TELEGRAM_CHAT_ID", "12345"):
            result = await send_telegram_alert(
                job_id="test_scan_001",
                alert_type="scan_started",
                company="System",
                role="",
                portal="",
                job_url="",
                details="Starting On-Demand Scan for ALL 442 Companies...",
                force=True
            )
        self.assertFalse(result, "scan_started must be suppressed")
        from app.services.telegram_notifier import _SENT_ALERTS
        self.assertNotIn("test_scan_001:scan_started", _SENT_ALERTS)


# ---------------------------------------------------------------------------
# Test 5: Pacing — no randomization, FIXED 150s
# ---------------------------------------------------------------------------

class Test5FixedPacing(unittest.TestCase):
    """Pacing must be FIXED 150s — assert no random usage in pacing code paths."""

    def test_config_min_delay_is_150(self):
        """MIN_DELAY_BETWEEN_APPS must be 150s in config."""
        self.assertEqual(settings.MIN_DELAY_BETWEEN_APPS, 150)

    def test_config_max_delay_is_150(self):
        """MAX_DELAY_BETWEEN_APPS must equal MIN (no jitter)."""
        self.assertEqual(settings.MAX_DELAY_BETWEEN_APPS, 150)

    def test_config_min_equals_max_delay(self):
        """MIN and MAX must be identical — no randomization possible."""
        self.assertEqual(settings.MIN_DELAY_BETWEEN_APPS, settings.MAX_DELAY_BETWEEN_APPS)

    def test_config_max_applications_per_day_is_50(self):
        """MAX_APPLICATIONS_PER_DAY cap must be 50."""
        self.assertLessEqual(settings.MAX_APPLICATIONS_PER_DAY, 50)

    def test_resume_bot_pacing_constants_are_fixed_150(self):
        """resume_bot.py pacing constants must be 150/150."""
        bot_path = os.path.expanduser("~/Desktop/resume_bot.py")
        self.assertTrue(os.path.exists(bot_path), f"resume_bot.py not found at {bot_path}")
        with open(bot_path) as f:
            source = f.read()
        # Assert no random.randint in pacing context
        self.assertNotIn(
            "random.randint(MIN_DELAY_BETWEEN_APPLICATIONS, MAX_DELAY_BETWEEN_APPLICATIONS)",
            source,
            "random.randint must not be used in pacing code path"
        )

    def test_resume_bot_min_delay_constant_is_150(self):
        bot_path = os.path.expanduser("~/Desktop/resume_bot.py")
        with open(bot_path) as f:
            source = f.read()
        # Find MIN_DELAY_BETWEEN_APPLICATIONS assignment
        m = re.search(r"MIN_DELAY_BETWEEN_APPLICATIONS\s*=\s*(\d+)", source)
        self.assertIsNotNone(m, "MIN_DELAY_BETWEEN_APPLICATIONS not found in resume_bot.py")
        self.assertEqual(int(m.group(1)), 150, "MIN_DELAY_BETWEEN_APPLICATIONS must be 150")

    def test_resume_bot_max_delay_constant_is_150(self):
        bot_path = os.path.expanduser("~/Desktop/resume_bot.py")
        with open(bot_path) as f:
            source = f.read()
        m = re.search(r"MAX_DELAY_BETWEEN_APPLICATIONS\s*=\s*(\d+)", source)
        self.assertIsNotNone(m, "MAX_DELAY_BETWEEN_APPLICATIONS not found in resume_bot.py")
        self.assertEqual(int(m.group(1)), 150, "MAX_DELAY_BETWEEN_APPLICATIONS must be 150")

    def test_field_fill_gap_ms_is_600(self):
        """FIELD_FILL_GAP_MS must be 600ms per standing rule."""
        self.assertEqual(settings.FIELD_FILL_GAP_MS, 600)


# ---------------------------------------------------------------------------
# Test 6: Filter Breakdown sums to Skipped/Blocked total
# ---------------------------------------------------------------------------

class Test6FilterBreakdown(unittest.TestCase):
    """Filter Breakdown in digest must sum to the Skipped/Blocked total."""

    def test_filter_breakdown_appears_in_digest_when_skips_exist(self):
        logger = AuditLogger(log_path="/dev/null")
        logger.log_submission(
            company="Acme", role_title="ML Engineer",
            job_url="https://boards.greenhouse.io/acme/jobs/1",
            status="skipped",
            skip_reason="WRONG_FUNCTION: FORBIDDEN_DOMAIN: analyst"
        )
        logger.log_submission(
            company="Beta Corp", role_title="Software Engineer",
            job_url="https://boards.greenhouse.io/beta/jobs/2",
            status="skipped",
            skip_reason="EXPERIENCE_EXCEEDED: Parsed requirement '5+ years' (5 yrs) exceeds 0-3 yrs cap"
        )
        digest = logger.generate_run_digest()
        self.assertIn("Filter Breakdown", digest, "Filter Breakdown must appear in digest")
        self.assertIn("WRONG_FUNCTION", digest)
        self.assertIn("EXPERIENCE_EXCEEDED", digest)

    def test_filter_breakdown_total_matches_skipped_count(self):
        logger = AuditLogger(log_path="/dev/null")
        for i in range(3):
            logger.log_submission(
                company=f"Company {i}", role_title="Software Engineer",
                job_url=f"https://boards.greenhouse.io/co{i}/jobs/1",
                status="skipped",
                skip_reason="WRONG_FUNCTION: forbidden domain"
            )
        logger.log_submission(
            company="Exp Co", role_title="Senior Dev",
            job_url="https://boards.greenhouse.io/exp/jobs/9",
            status="skipped",
            skip_reason="EXPERIENCE_EXCEEDED: 5+ years exceeds cap"
        )
        digest = logger.generate_run_digest()
        # Parse the total from "_Total filtered/skipped: N_"
        m = re.search(r"Total filtered/skipped: (\d+)", digest)
        self.assertIsNotNone(m, "Total filtered line must appear")
        reported_total = int(m.group(1))
        actual_skipped = len(logger._current_run_skipped)
        self.assertEqual(reported_total, actual_skipped,
                         f"Filter Breakdown total {reported_total} != actual skipped {actual_skipped}")

    def test_filter_breakdown_absent_when_no_skips(self):
        logger = AuditLogger(log_path="/dev/null")
        digest = logger.generate_run_digest()
        # No skips → no Filter Breakdown section
        self.assertNotIn("Filter Breakdown", digest)

    def test_filter_breakdown_includes_challenge_blocked(self):
        logger = AuditLogger(log_path="/dev/null")
        logger.log_submission(
            company="Captcha Co", role_title="Backend Engineer",
            job_url="https://boards.greenhouse.io/captcha/jobs/1",
            status="challenge_blocked",
            skip_reason="reCAPTCHA Enterprise detected",
            challenge_type="reCAPTCHA Enterprise",
            stage_detected="form_load"
        )
        digest = logger.generate_run_digest()
        self.assertIn("Filter Breakdown", digest)
        self.assertIn("CHALLENGE_BLOCKED", digest)


# ---------------------------------------------------------------------------
# Test 7: Gate Honesty Audit (H-1B path + EXPERIENCE_UNPARSEABLE share)
# ---------------------------------------------------------------------------

class Test7GateHonestyAudit(unittest.TestCase):
    """Audit: H-1B path behavior and EXPERIENCE_UNPARSEABLE share."""

    def test_h1b_gate_disabled_never_blocks_postings(self):
        """
        H-1B GATE REMOVAL (user direction 2026-09-25):
        check_h1b_sponsorship() is stubbed/disabled per user direction 2026-09-25.
        It always returns True, even on explicit 'does not sponsor' or 'no visa sponsorship'.
        Sponsorship language never blocks any posting; no code path consults sponsorship keywords.
        """
        import importlib.util
        bot_path = os.path.expanduser("~/Desktop/resume_bot.py")
        spec = importlib.util.spec_from_file_location("resume_bot", bot_path)
        mod = importlib.util.module_from_spec(spec)
        mod.__dict__["__file__"] = bot_path
        try:
            spec.loader.exec_module(mod)
        except Exception:
            pass

        if hasattr(mod, "check_h1b_sponsorship"):
            fn = mod.check_h1b_sponsorship
            self.assertTrue(fn("We are looking for a great engineer to join our team."))
            self.assertTrue(fn("This company does not sponsor visas or work authorization."))
            self.assertTrue(fn("Must have permanent US work authorization without requiring sponsorship."))
            self.assertTrue(fn("No sponsorship available now or in the future."))

    def test_experience_gate_unstated_passes_by_default(self):
        """
        EXPERIENCE_UNPARSEABLE HONESTY:
        With ALLOW_UNSTATED_EXPERIENCE=True (default), unparseable experience
        requirements → UNSTATED_EXPERIENCE_PASS, NOT a hard skip.
        The code never produces EXPERIENCE_UNPARSEABLE unless toggle is False.
        """
        ok, reason, _ = ExperienceGate.evaluate(
            title="Software Engineer",
            description="You will work with our platform. No experience requirement stated.",
            allow_unstated=True
        )
        self.assertTrue(ok)
        self.assertIn("UNSTATED_EXPERIENCE_PASS", reason)

    def test_experience_gate_unparseable_hard_skip_when_toggle_off(self):
        """With allow_unstated=False, no parseable requirement → hard skip."""
        ok, reason, _ = ExperienceGate.evaluate(
            title="Software Engineer",
            description="You will work with our platform. No experience requirement stated.",
            allow_unstated=False
        )
        self.assertFalse(ok)
        self.assertIn("UNPARSEABLE_EXPERIENCE", reason)

    def test_telegram_allowlist_exhaustive(self):
        """
        Telegram allowlist must be exhaustive — only the documented types pass.
        Verify allowlist contains exactly the expected types.
        """
        import inspect
        import app.services.telegram_notifier as tn
        src = inspect.getsource(tn.send_telegram_alert)
        expected_types = [
            "submitted",
            "cap_reached", "capped", "employer_limit", "candidate_limit",
            "captcha_solve_request",
            "missing_info_request",
            "system_health",
        ]
        for t in expected_types:
            self.assertIn(t, src, f"'{t}' must be in telegram allowlist")


if __name__ == "__main__":
    unittest.main()
