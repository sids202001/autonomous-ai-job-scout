"""
Comprehensive Unit Tests for Nov 16-17 Audit Global Fixes (Rules 1-15).
Covers:
1. Clear-before-fill & read-back verification (FillVerificationFailedError & retry)
2. Banner dismissal least-permissive priority & BANNER_BLOCKED
3. Relocation / in-office / hub safe-halt (RELOCATION_DECISION_REQUIRED & Telegram MISSING_INFO_REQUEST formatting)
4. "Which Visa?" canonical H-1B resolution & VISA_OPTION_MISSING
5. Essays, custom subjective questions & self-ratings safe-halt (ESSAY_HALT)
6. Contradictory-premise questions safe-halt (PREMISE_MISMATCH)
7. Home address safe-halt (ADDRESS_REQUIRED)
8. "How did you hear about us?" company website priority & SOURCE_OPTION_MISSING
9. Communications consent fine-grained opt-in / opt-out / CONSENT_AMBIGUOUS
10. Full JD HTML cleaning & multi-requirement 4+ year gate (Dropbox AppSec fix)
11. Hardware / facilities role family exclusion patterns
12. Terminal blocking in retry_queue on halted codes
"""
import unittest
import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.form_guards import (
    FormHaltException,
    FillVerificationFailedError,
    is_relocation_or_in_office_question,
    is_relocation_willingness_question,
    is_current_location_or_proximity_question,
    solve_which_visa,
    is_which_visa_question,
    is_essay_or_subjective_question,
    is_contradictory_premise_question,
    is_home_address_field,
    solve_how_did_you_hear,
    is_how_did_you_hear_question,
    solve_consent_question,
    format_missing_info_request,
)
from app.services.experience_gate import (
    clean_jd_text,
    ExperienceGate,
    ROLE_FAMILY_EXCLUSION_PATTERNS,
)
from form_filler import DeterministicEngine, HybridATSFormFiller, dismiss_cookie_banners
from app.services.retry_queue import RetryQueueManager


class TestAuditGlobalFixes(unittest.TestCase):

    # =========================================================================
    # Rule 1 & Startup Verification: profile.json source of truth
    # =========================================================================
    def test_rule_1_zero_occurrences_of_old_github_in_repo(self):
        """Rule 1: Grep check that old handle is completely purged for github."""
        import subprocess
        forbidden_target = "github.com/" + "siddhesh202001"
        res = subprocess.run(
            ["grep", "-rn", "--exclude=test_audit_global_fixes.py", "--exclude=*.md", "--exclude=*.pyc", forbidden_target, "/Users/siddheshsawant/sid_job_scrip"],
            capture_output=True,
            text=True
        )
        self.assertEqual(res.stdout.strip(), "", f"Found forbidden old github URL occurrences: {res.stdout}")

    def test_rule_1_profile_verifier_passes(self):
        """Rule 1: Startup profile verifier confirms canonical profile and URLs."""
        from app.services.profile_verifier import run_startup_self_check
        ok, msg = run_startup_self_check()
        self.assertTrue(ok, f"Profile verification failed: {msg}")
        self.assertIn("PROFILE_URL_CHECK_PASSED", msg)

    # =========================================================================
    # Rule 2: Clear-Before-Fill & Read-Back Verification ("Talviro INCviro INC")
    # =========================================================================
    def test_rule_2_clear_before_fill_read_back_retry_and_halt(self):
        """Rule 2: Simulate field mismatch (Talviro INCviro INC). Must retry once, then raise FillVerificationFailedError."""
        mock_page = AsyncMock()
        mock_page.screenshot = AsyncMock()
        profile_data = {"personal_info": {"full_name": "Siddhesh Santosh Sawant"}}
        filler = HybridATSFormFiller(mock_page, profile_data, "lever")

        mock_locator = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.first = mock_locator
        mock_locator.click = AsyncMock()
        mock_locator.press = AsyncMock()
        mock_locator.fill = AsyncMock()
        mock_locator.dispatch_event = AsyncMock()
        
        # Simulate read-back corruption: field keeps returning corrupted text
        mock_locator.input_value = AsyncMock(return_value="Talviro INCviro INC")

        with tempfile.TemporaryDirectory() as tmpdir:
            screenshot_path = os.path.join(tmpdir, "verification_fail.png")
            with patch("form_filler.os.path.join", return_value=screenshot_path):
                with self.assertRaises(FillVerificationFailedError) as ctx:
                    asyncio.run(filler._human_fill(mock_locator, "Talviro INC", field_name="Current Company"))
                self.assertEqual(ctx.exception.code, "FILL_VERIFICATION_FAILED")
                self.assertEqual(ctx.exception.intended, "Talviro INC")
                self.assertEqual(ctx.exception.actual, "Talviro INCviro INC")
                # Verify at least 2 attempts were made before halting (initial + 1 retry)
                self.assertGreaterEqual(mock_locator.input_value.await_count, 2)

    # =========================================================================
    # Rule 3: Banner Dismissal Least-Permissive Priority & BANNER_BLOCKED
    # =========================================================================
    def test_rule_3_banner_dismissal_priority_and_blocking_halt(self):
        """Rule 3: Least-permissive banner dismissal. If banner remains visible, raises BANNER_BLOCKED."""
        mock_page = AsyncMock()
        mock_page.screenshot = AsyncMock()
        
        # Simulate banner remaining visible and blocking
        mock_banner = AsyncMock()
        mock_banner.count = AsyncMock(return_value=1)
        mock_banner.first = mock_banner
        mock_banner.is_visible = AsyncMock(return_value=True)
        mock_banner.bounding_box = AsyncMock(return_value={"width": 400, "height": 150})
        mock_banner.click = AsyncMock(side_effect=Exception("Click intercepted by overlay"))
        
        mock_page.locator = MagicMock(return_value=mock_banner)

        with self.assertRaises(FormHaltException) as ctx:
            asyncio.run(dismiss_cookie_banners(mock_page, raise_on_blocked=True))
        self.assertEqual(ctx.exception.code, "BANNER_BLOCKED")

    # =========================================================================
    # Rule 4 & Addendum (Nov 18): Relocation Willingness & Proximity Guards
    # =========================================================================
    def test_rule_4_relocation_willingness_answers_canonical_yes(self):
        """Rule 4 Addendum: Relocation willingness questions answer Yes from profile.json (never halt)."""
        willingness_questions = [
            ("Are you willing to work from our San Francisco office 3 days a week?", ["Yes", "No"]),
            ("Are you willing to relocate to Seattle, WA?", ["Yes", "No"]),
            ("This role requires 3 days per week on-site in New York. Do you acknowledge?", ["Yes", "No"]),
            ("Are you open to a hybrid work schedule?", ["Yes", "No"]),
            ("Are you willing to relocate for this role?", ["Yes", "No"]),
            ("Are you able to work from our office five days a week?", ["Yes", "No"]),
            ("Office culture requires daily in-person attendance. Do you agree?", ["Yes", "No"]),
            ("Are you local to or willing to relocate?", ["Yes", "No"]),
            ("Would you relocate for this opportunity?", ["Willing to relocate", "Not willing"]),
        ]

        for q, choices in willingness_questions:
            self.assertTrue(is_relocation_willingness_question(q, choices), f"Failed to identify as willingness question: {q}")
            ans = DeterministicEngine.solve(q, choices)
            self.assertIsNotNone(ans, f"Expected answer for {q}")
            self.assertTrue(any(w in ans.lower() for w in ["yes", "willing", "relocate", "agree"]), f"Answer '{ans}' was not affirmative for {q}")

        # Free text response from profile.json
        free_text_ans = DeterministicEngine.solve("Are you willing to relocate?")
        self.assertIn("Yes", free_text_ans)
        self.assertIn("United States", free_text_ans)

    def test_rule_4_current_location_and_50_miles_truthful_dc_and_halts(self):
        """Rule 4: Current location and 50-miles questions answer truthfully based on DC profile, never selecting non-DC hubs."""
        # 1. Non-DC hub with Yes/No choices -> answers "No"
        ans_sunnyvale = DeterministicEngine.solve("Are you located within 50 miles of our Sunnyvale office?", ["Yes", "No"])
        self.assertEqual(ans_sunnyvale, "No", "Candidate lives in DC, must answer 'No' to Sunnyvale 50 miles")

        # 2. DC hub with Yes/No choices -> answers "Yes"
        ans_dc = DeterministicEngine.solve("Are you located within 50 miles of our Washington, DC office?", ["Yes", "No"])
        self.assertEqual(ans_dc, "Yes", "Candidate lives in DC, must answer 'Yes' to DC 50 miles")

        # 3. Hub choices offering Remote -> selects Remote
        ans_pref = DeterministicEngine.solve("Which office location do you prefer?", ["San Francisco (Union Square)", "New York", "Remote"])
        self.assertEqual(ans_pref, "Remote")

        # 4. Hub choices offering ONLY non-DC hubs without Remote/None -> HALTS with HUB_NOT_NEAR_RESIDENCE
        writer_choices = ["San Francisco (Union Square)", "New York", "London"]
        with self.assertRaises(FormHaltException) as ctx:
            DeterministicEngine.solve("Are you located within 50 miles of one of the following office hubs", writer_choices)
        self.assertIn(ctx.exception.code, ["HUB_NOT_NEAR_RESIDENCE", "PROXIMITY_REQUIREMENT_UNMET"])

    def test_rule_4_relocation_assistance_and_timing_safe_halt(self):
        """Rule 4 (updated Nov 22): Relocation timing still halts; relocation assistance & remote work now use canonical answers from profile.json."""
        self.assertEqual(DeterministicEngine.solve("Do you require relocation assistance?", ["Yes", "No"]), "No")
        self.assertEqual(DeterministicEngine.solve("Are you open to relocation assistance if the company provides it?", ["Yes", "No"]), "Yes")

        with self.assertRaises(FormHaltException) as ctx:
            DeterministicEngine.solve("When would you be able to relocate?", ["Immediately", "In 1 month", "In 3 months"])
        self.assertEqual(ctx.exception.code, "RELOCATION_TIMING_HALT")

        self.assertEqual(DeterministicEngine.solve("Are you only willing to work fully remote?", ["Yes", "No"]), "Yes")

    def test_rule_4_telegram_missing_info_request_format(self):
        """Rule 4: Telegram MISSING_INFO_REQUEST format is human-actionable with question, URL, choices, 24h window."""
        msg = format_missing_info_request(
            code="HUB_NOT_NEAR_RESIDENCE",
            question="Are you located within 50 miles of one of the following office hubs",
            url="https://jobs.ashbyhq.com/writer/123",
            company="Writer",
            choices=["San Francisco (Union Square)", "New York"]
        )
        self.assertIn("Office Hub / Proximity Decision", msg)
        self.assertIn("Are you located within 50 miles of one of the following office hubs", msg)
        self.assertIn("https://jobs.ashbyhq.com/writer/123", msg)
        self.assertIn("Options Offered:", msg)
        self.assertIn("24-Hour", msg)

    # =========================================================================
    # Rule 5: "Which Visa?" = H-1B
    # =========================================================================
    def test_rule_5_which_visa_canonical_h1b(self):
        """Rule 5: Selects H-1B when present. If H-1B missing, halts with VISA_OPTION_MISSING."""
        q = "Which visa or sponsorship type will you require in the future?"
        self.assertTrue(is_which_visa_question(q))
        
        # When H-1B is offered
        choices_with_h1b = ["TN Visa", "H-1B", "E-3", "F-1 (OPT)"]
        ans = solve_which_visa(q, choices_with_h1b)
        self.assertEqual(ans, "H-1B")

        # When H-1B is missing -> Must halt with VISA_OPTION_MISSING
        choices_without_h1b = ["TN Visa", "E-3 Visa", "O-1 Visa", "L-1 Visa"]
        with self.assertRaises(FormHaltException) as ctx:
            solve_which_visa(q, choices_without_h1b)
        self.assertEqual(ctx.exception.code, "VISA_OPTION_MISSING")

    # =========================================================================
    # Rule 6: Open-Text Hard Limits Safe-Halt vs Grounded Open-Text Answers
    # =========================================================================
    def test_rule_6_essays_and_ratings_safe_halt(self):
        """Rule 6 (updated Nov 22): Hard-limit open-text questions (ratings, recent reading history, abstract philosophy,
        or company/role questions without JD context) halt with ESSAY_HALT; groundable project/bug/experience questions
        write grounded answers from the candidate's verified record."""
        from app.services.grounded_essay import clear_application_context
        clear_application_context()

        hard_limit_questions = [
            "Why are you a good fit for this role at Anthropic?",  # No Anthropic JD in context -> halts
            "What is the most interesting paper or blog post you read recently?",  # Reading history not in record -> halts
            "Rate your proficiency in Python from 1 to 10.",  # Self-rating scale -> halts
            "Why do you want to work at Writer?",  # No Writer JD in context -> halts
        ]

        for q in hard_limit_questions:
            self.assertTrue(is_essay_or_subjective_question(q), f"Failed to identify essay/rating question: {q}")
            with self.assertRaises(FormHaltException) as ctx:
                DeterministicEngine.solve(q, is_required=True)
            self.assertEqual(ctx.exception.code, "ESSAY_HALT")

            # Optional hard-limit questions return empty string and do not halt
            ans_opt = DeterministicEngine.solve(q, is_required=False)
            self.assertEqual(ans_opt, "")

        # Groundable candidate-record open-text questions now write grounded 2-4 sentence answers
        for q_groundable in [
            "Describe your proudest engineering accomplishment.",
            "Explain a challenging technical bug you debugged and solved.",
        ]:
            ans = DeterministicEngine.solve(q_groundable, is_required=True)
            self.assertTrue(isinstance(ans, str) and len(ans) > 60, f"Expected grounded answer for {q_groundable}, got: {ans}")

    # =========================================================================
    # Rule 7: Contradictory-Premise Questions Safe-Halt
    # =========================================================================
    def test_rule_7_contradictory_premise_safe_halt(self):
        """Rule 7: Contradictory premise (e.g. international follow-up for US resident)."""
        q = "If you selected international, what location(s) are you interested in?"
        self.assertTrue(is_contradictory_premise_question(q))
        
        with self.assertRaises(FormHaltException) as ctx:
            DeterministicEngine.solve(q, is_required=True)
        self.assertEqual(ctx.exception.code, "PREMISE_MISMATCH")

        # Optional contradictory field returns empty string
        ans_opt = DeterministicEngine.solve(q, is_required=False)
        self.assertEqual(ans_opt, "")

    # =========================================================================
    # Rule 8: Home Address Safe-Halt (Never Guessed, Never Partial)
    # =========================================================================
    def test_rule_8_home_address_safe_halt_when_not_stored(self):
        """Rule 8: Required home/street address halts with ADDRESS_REQUIRED when not in profile.json."""
        q = "Home Street Address"
        self.assertTrue(is_home_address_field(q))

        with patch("form_filler.load_profile", return_value={"personal_info": {"home_address": ""}}):
            with self.assertRaises(FormHaltException) as ctx:
                DeterministicEngine.solve(q, is_required=True)
        self.assertEqual(ctx.exception.code, "ADDRESS_REQUIRED")

        # When address is present in profile, it is filled verbatim
        custom_profile = {"personal_info": {"home_address": "123 Main St, Apt 4B"}}
        with patch("form_filler.load_profile", return_value=custom_profile):
            ans = DeterministicEngine.solve(q, is_required=True)
            self.assertEqual(ans, "123 Main St, Apt 4B")

    # =========================================================================
    # Rule 9: "How Did You Hear About Us?": Company Website First, Never LinkedIn
    # =========================================================================
    def test_rule_9_how_did_you_hear_priority_and_banned_sources(self):
        """Rule 9: Company Website > Job Board > Other ('Company careers page'). Never LinkedIn/Referral."""
        q = "How did you hear about this job opportunity?"
        self.assertTrue(is_how_did_you_hear_question(q))

        # Priority 1: Company Website
        choices_1 = ["LinkedIn", "Company Website", "Employee Referral", "Recruiter"]
        self.assertEqual(solve_how_did_you_hear(q, choices_1), "Company Website")

        # Priority 2: Job Board
        choices_2 = ["LinkedIn", "Job Board", "Internal Referral"]
        self.assertEqual(solve_how_did_you_hear(q, choices_2), "Job Board")

        # Priority 3: Other
        choices_3 = ["LinkedIn", "Referral", "Other"]
        self.assertEqual(solve_how_did_you_hear(q, choices_3), "Other")

        # Banned options only -> Halts with SOURCE_OPTION_MISSING
        choices_banned = ["LinkedIn", "Employee Referral", "Headhunter Agency"]
        with self.assertRaises(FormHaltException) as ctx:
            solve_how_did_you_hear(q, choices_banned, is_required=True)
        self.assertEqual(ctx.exception.code, "SOURCE_OPTION_MISSING")

    # =========================================================================
    # Rule 10: Communications Consent
    # =========================================================================
    def test_rule_10_communications_consent(self):
        """Rule 10: Status updates = Yes, Marketing = No, Bundled = CONSENT_AMBIGUOUS."""
        # Application updates -> Yes
        ans_status = solve_consent_question("Consent to receive text messages regarding application status", ["Yes", "No"])
        self.assertEqual(ans_status, "Yes")

        # Marketing / Talent pool -> No
        ans_mkt = solve_consent_question("Receive promotional marketing emails and talent community newsletters", ["Yes", "No"])
        self.assertEqual(ans_mkt, "No")

        # Bundled consent -> CONSENT_AMBIGUOUS
        bundled_q = "I consent to receive application status updates and marketing newsletters from our talent team"
        with self.assertRaises(FormHaltException) as ctx:
            solve_consent_question(bundled_q, ["Yes", "No"])
        self.assertEqual(ctx.exception.code, "CONSENT_AMBIGUOUS")

    # =========================================================================
    # Rule 11: JD Cleaning, Role Exclusion, and Experience Filter (Dropbox AppSec fix)
    # =========================================================================
    def test_rule_11_clean_jd_text_strips_html_tags_and_entities(self):
        """Rule 11: clean_jd_text converts HTML tags and entities to plain text."""
        raw_html = "<p>Qualifications: &bull; Requires <strong>4+ years</strong> of experience in application security.</p>"
        cleaned = clean_jd_text(raw_html)
        self.assertIn("Requires 4+ years of experience in application security.", cleaned)
        self.assertNotIn("<p>", cleaned)
        self.assertNotIn("<strong>", cleaned)
        self.assertNotIn("&bull;", cleaned)

    def test_rule_11_dropbox_appsec_4_plus_years_disqualification(self):
        """Rule 11: Detects 4+ years in cleaned JD and rejects role for candidate (max 3 years)."""
        dropbox_appsec_jd = """
        About the Role:
        Dropbox is seeking an Application Security Engineer to protect our infrastructure.
        Minimum Qualifications:
        - 4+ years of professional software engineering or application security experience
        - B.S. or M.S. in Computer Science or equivalent
        - Experience with vulnerability management and threat modeling
        """
        reqs = ExperienceGate._parse_all_experience_requirements(dropbox_appsec_jd)
        self.assertTrue(any(r[0] >= 4 for r in reqs), f"Expected 4+ years requirement in {reqs}")
        is_eligible, reason, meta = ExperienceGate.evaluate("Application Security Engineer", dropbox_appsec_jd)
        self.assertFalse(is_eligible)
        self.assertIn("exceeds", reason.lower())

    def test_rule_11_role_family_exclusion_hardware_and_facilities(self):
        """Rule 11: Disqualifies hardware, data center technician, facilities, and physical cabling roles."""
        test_titles = [
            "Data Center Facilities Engineer",
            "Hardware Infrastructure Technician",
            "Data Center Operations Technician",
            "Cable Plant Systems Engineer",
            "HVAC / Electrical Facilities Specialist",
        ]
        for title in test_titles:
            is_eligible, reason, meta = ExperienceGate.evaluate(title, "General JD content")
            self.assertFalse(is_eligible, f"Title was not disqualified: {title}")
            self.assertIn("excluded", reason.lower())

    # =========================================================================
    # Rule 12: Terminal Blocking in Retry Queue on Halted Codes
    # =========================================================================
    def test_rule_12_retry_queue_terminal_blocking(self):
        """Rule 12: Applications halted with safety codes are permanently blocked with terminal=1."""
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            mgr = RetryQueueManager(db_path=tmp.name)
            test_halt_codes = [
                "RELOCATION_DECISION_REQUIRED",
                "VISA_OPTION_MISSING",
                "ESSAY_HALT",
                "PREMISE_MISMATCH",
                "ADDRESS_REQUIRED",
                "SOURCE_OPTION_MISSING",
                "CONSENT_AMBIGUOUS",
                "FILL_VERIFICATION_FAILED",
                "BANNER_BLOCKED",
                "UPLOAD_UNVERIFIED",
                "EVIDENCE_FAILURE",
                "CHALLENGE_BLOCKED",
            ]
            for i, code in enumerate(test_halt_codes):
                company = f"Company_{i}"
                title = "Software Engineer"
                url = f"https://jobs.lever.co/company_{i}/123"
                mgr.record_attempt_start(company, title, url)
                enqueued, outcome = mgr.record_attempt_result(
                    company, title, url, status=code, reason=f"Halted due to {code}"
                )
                self.assertFalse(enqueued, f"Halt code {code} must NOT be enqueued for auto-retry")
                self.assertIn("TERMINAL_BLOCKED", outcome)

                # Verify can_attempt_role rejects it immediately as terminal
                can_attempt, reason, count = mgr.can_attempt_role(company, title, url)
                self.assertFalse(can_attempt)
                self.assertIn("TERMINAL", reason)

    # =========================================================================
    # Rule 12 & Addendum (Nov 18): Cap Counts Submissions Only & Circuit Breaker
    # =========================================================================
    def test_rule_12_company_cap_counts_submissions_only(self):
        """Rule 12 Addendum: Only status=='submitted' consumes a cap slot. Blocked/halted attempts do NOT consume cap."""
        from app.services.company_cap import CompanyCapTracker
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            tracker = CompanyCapTracker(db_path=tmp.name)
            company = "TestCapAccountingCo"
            url = "https://jobs.lever.co/testcap/123"

            # 1. Halted / Challenge-blocked attempt
            tracker.record_submission(company, "Role 1", url, status="CHALLENGE_BLOCKED", match_reason="Turnstile detected")
            is_capped, reason, prior_count = tracker.is_company_capped(company, url)
            self.assertFalse(is_capped, "Challenge-blocked attempt must NOT consume cap slot")
            self.assertEqual(prior_count, 0, "Prior submitted count must be 0")

            # 2. Another halted attempt (e.g. ESSAY_HALT)
            tracker.record_submission(company, "Role 2", url, status="ESSAY_HALT", match_reason="Essay required")
            # Note: circuit breaker paused check will trigger if 2 halts within 24h, tested below.
            # But in terms of submission cap count:
            with tracker._get_connection() as conn:
                sub_count = conn.execute("SELECT COUNT(*) FROM company_submissions WHERE company_norm = ? AND status = 'submitted'", ("testcapaccountingco",)).fetchone()[0]
                self.assertEqual(sub_count, 0, "Non-submitted attempts must NEVER be recorded as status='submitted'")

            # 3. Successful submission consumes 1 slot
            tracker.record_submission(company, "Role 3", url, status="submitted", match_reason="Verified")
            with tracker._get_connection() as conn:
                sub_count = conn.execute("SELECT COUNT(*) FROM company_submissions WHERE company_norm = ? AND status = 'submitted'", ("testcapaccountingco",)).fetchone()[0]
                self.assertEqual(sub_count, 1, "Verified submission must consume exactly 1 slot")

    def test_rule_12_company_circuit_breaker_24h_pause(self):
        """Rule 12 Addendum: 2 terminal halts/challenges within 24h triggers 24h pause on NEW launches, logging COMPANY_PAUSED."""
        from app.services.company_cap import CompanyCapTracker
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            tracker = CompanyCapTracker(db_path=tmp.name)
            company = "AggregatorFlooder"
            url1 = "https://jobs.lever.co/aggregator/role1"
            url2 = "https://jobs.lever.co/aggregator/role2"
            url3 = "https://jobs.lever.co/aggregator/role3"

            # 1st attempt ends in CHALLENGE_BLOCKED
            tracker.record_terminal_halt(company, url1, reason="CHALLENGE_BLOCKED: Turnstile detected")
            is_paused, _ = tracker.is_company_paused(company, url1)
            self.assertFalse(is_paused, "Single halt must not trigger circuit breaker")

            # 2nd attempt ends in RELOCATION_DECISION_REQUIRED or other terminal halt
            with self.assertLogs("app.services.company_cap", level="WARNING") as cm:
                tracker.record_terminal_halt(company, url2, reason="PROXIMITY_REQUIREMENT_UNMET: Hub non-DC")
            self.assertTrue(any("COMPANY_PAUSED" in log for log in cm.output), "Must log COMPANY_PAUSED when 2 halts occur within 24h")

            # Verify circuit breaker is now active for this company
            is_paused, pause_reason = tracker.is_company_paused(company, url3)
            self.assertTrue(is_paused, "Company must be paused after 2 halts in 24h")
            self.assertIn("COMPANY_PAUSED", pause_reason)

            # is_company_capped also blocks new launches due to pause
            is_capped, cap_reason, _ = tracker.is_company_capped(company, url3)
            self.assertTrue(is_capped)
            self.assertIn("COMPANY_PAUSED", cap_reason)

            # Cap count remains 0 because halts do NOT consume submission cap slots
            with tracker._get_connection() as conn:
                sub_count = conn.execute("SELECT COUNT(*) FROM company_submissions WHERE company_norm = ? AND status = 'submitted'", ("aggregatorflooder",)).fetchone()[0]
                self.assertEqual(sub_count, 0, "Circuit breaker pause must not consume submission cap slots")


if __name__ == "__main__":
    unittest.main()
