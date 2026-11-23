import calendar
from datetime import datetime
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

repo_dir = str(Path(__file__).resolve().parent.parent)
if repo_dir not in sys.path:
    sys.path.insert(0, repo_dir)

from form_filler import (
    DeterministicEngine,
    compute_start_date,
    load_profile,
    normalize_label,
    resolve_start_date,
)
from app.services.llm_verifier import llm_pre_submit_audit


class TestJobScoutFinalHardening(unittest.TestCase):
    """
    Test suite validating all Job Scout fixes:
    1. Extraction failure returns EXTRACTION_FAILED, never resume_attached=False.
    2. Salary and EEO values are dynamically read from profile.json, never hardcoded.
    3. Filled start date equals submit date + 1 month (rule-based, never hardcoded date).
    4. Ashby extraction path, phone selectors, consent handling, and education resolving.
    5. Inviolable canonical answers (US work-auth, sponsorship, ITAR, government employment).
    """

    # =========================================================================
    # 1. Start Date Rule (Submit Date + 1 Month)
    # =========================================================================
    def test_filled_start_date_equals_submit_date_plus_one_month(self):
        """Verify filled start date equals the application's submit date + 1 month."""
        # Standard calendar progression
        base_oct20 = datetime(2026, 10, 20)
        self.assertEqual(compute_start_date(base_oct20), "2026-11-20")

        # Month boundary handling (Jan 31 -> Feb 28 in non-leap year)
        base_jan31 = datetime(2026, 1, 31)
        self.assertEqual(compute_start_date(base_jan31), "2026-02-28")

        # Leap year handling (Jan 31, 2024 -> Feb 29, 2024)
        base_leap = datetime(2024, 1, 31)
        self.assertEqual(compute_start_date(base_leap), "2024-02-29")

        # Year rollover (Dec 15, 2026 -> Jan 15, 2027)
        base_dec = datetime(2026, 12, 15)
        self.assertEqual(compute_start_date(base_dec), "2027-01-15")

        # Dynamic fill time calculation (today + 1 month)
        today = datetime.now()
        expected_today = compute_start_date(today)
        self.assertEqual(resolve_start_date(), expected_today)

    def test_start_date_rule_key_in_profile_and_no_halts(self):
        """Verify profile.json stores start date as a rule, and text/date fields get computed date."""
        profile = load_profile()
        prefs = profile.get("preferences", {})

        # Standing rule key must be present and must not be a hardcoded calendar date
        self.assertIn("start_date_rule", prefs)
        self.assertEqual(prefs["start_date_rule"], "submit_date + 1 month")
        self.assertNotIn("2026-", str(prefs["start_date_rule"]))

        expected_date = compute_start_date()

        # Both text fields ("when can you start?") and date fields resolve to the computed date
        questions = [
            "When can you start a new role?",
            "When would you be available to start?",
            "What is the earliest date you are available to start this position?",
            "Earliest start date",
            "When can you start?",
            "How soon can you start?",
            "Start date",
            "Availability to start",
        ]
        for q in questions:
            ans = DeterministicEngine.solve(q)
            self.assertEqual(
                ans,
                expected_date,
                f"Question '{q}' should resolve to computed start date '{expected_date}' but got '{ans}'"
            )

    def test_start_date_choices_selection(self):
        """Verify dropdown choices pick 1 month / computed date when available."""
        expected_date = compute_start_date()
        choices = [expected_date, "Immediately", "2 weeks", "2+ months"]
        ans = DeterministicEngine.solve("When can you start?", choices)
        self.assertEqual(ans, expected_date)

        choices_relative = ["Within 1 month", "2 weeks", "Immediately", "In 3 months"]
        ans_rel = DeterministicEngine.solve("When can you start?", choices_relative)
        self.assertIn("1 month", ans_rel.lower())

    # =========================================================================
    # 2. Dynamic Salary and EEO Read from profile.json
    # =========================================================================
    def test_salary_values_read_from_profile_never_hardcoded(self):
        """Verify salary is read directly from profile.json and never hardcoded."""
        # 1. Candidate's current profile ($100,000 / year)
        actual_profile = load_profile()
        self.assertEqual(actual_profile["compensation"]["annual_salary_expectation"], "100000")
        ans_actual = DeterministicEngine.solve("Expected Salary")
        self.assertEqual(ans_actual, "100000")

        # Salary brackets matching candidate's $100,000
        brackets = ["$80,000 - $95,000", "$95,000 - $115,000", "$120,000 - $140,000"]
        chosen_bracket = DeterministicEngine.solve("Desired Salary", brackets)
        self.assertEqual(chosen_bracket, "$95,000 - $115,000")

        # 2. Dynamic test: when profile changes to $130,000, answer moves accordingly
        custom_profile = json.loads(json.dumps(actual_profile))
        custom_profile["compensation"]["annual_salary_expectation"] = "130000"
        custom_profile["compensation"]["annual_salary_expectation_text"] = "$130,000 / year"
        custom_profile["compensation"]["hourly_rate_expectation"] = "75"

        with patch("form_filler.load_profile", return_value=custom_profile):
            ans_custom = DeterministicEngine.solve("Expected Salary")
            self.assertEqual(ans_custom, "130000")

            chosen_custom_bracket = DeterministicEngine.solve("Desired Salary", brackets)
            self.assertEqual(chosen_custom_bracket, "$120,000 - $140,000")

            ans_hourly = DeterministicEngine.solve("Desired hourly rate (dollar amount only)")
            self.assertEqual(ans_hourly, "75")

    def test_eeo_values_read_from_profile_never_hardcoded(self):
        """Verify EEO demographics (gender, race, veteran, disability, pronouns) are read from profile.json."""
        actual_profile = load_profile()
        comp = actual_profile["compliance_and_eeo"]

        # Canonical values confirmed by candidate
        self.assertEqual(comp["gender"], "Male")
        self.assertEqual(comp["race_ethnicity"], "Asian")
        self.assertEqual(comp["veteran_status"], "I am not a protected veteran")
        self.assertEqual(comp["disability_status"], "No, I do not have a disability")

        # Solves with candidate profile
        self.assertEqual(DeterministicEngine.solve("What is your gender?"), "Male")
        self.assertEqual(DeterministicEngine.solve("What is your race or ethnicity?"), "Asian")
        self.assertEqual(DeterministicEngine.solve("What is your military status?"), "I am not a protected veteran")
        self.assertEqual(DeterministicEngine.solve("What is your disability status?"), "No, I do not have a disability")

        # Choices alignment
        gender_choices = ["Female", "Male", "Non-binary", "Decline to state"]
        self.assertEqual(DeterministicEngine.solve("Gender Identity", gender_choices), "Male")

        race_choices = ["White / Caucasian", "Black or African American", "Asian (Not Hispanic or Latino)", "Two or more races"]
        self.assertEqual(DeterministicEngine.solve("Race / Ethnicity", race_choices), "Asian (Not Hispanic or Latino)")

        # Verify dynamic change when profile is modified
        custom_profile = json.loads(json.dumps(actual_profile))
        custom_profile["compliance_and_eeo"]["race_ethnicity"] = "South Asian"
        custom_profile["compliance_and_eeo"]["veteran_status"] = "Active Duty Military"
        with patch("form_filler.load_profile", return_value=custom_profile):
            self.assertEqual(DeterministicEngine.solve("What is your race or ethnicity?"), "South Asian")
            self.assertEqual(DeterministicEngine.solve("What is your military status?"), "Active Duty Military")

    # =========================================================================
    # 3. Phone Selectors, Consent, Education, and Canonical Answers
    # =========================================================================
    def test_phone_and_consent_handling(self):
        """Verify phone country code, phone selectors, and communication/SMS consent."""
        # Phone country code
        self.assertEqual(DeterministicEngine.solve("Country calling code"), "United States +1")
        phone_code_choices = ["United States +1", "Canada +1", "United Kingdom +44", "India +91"]
        self.assertEqual(DeterministicEngine.solve("Phone country code", phone_code_choices), "United States +1")

        # SMS / Communication consent
        self.assertEqual(
            DeterministicEngine.solve("Do you consent to receiving text messages regarding your application?"),
            "Yes - I consent to receiving text messages"
        )
        sms_choices = ["Yes - I consent to receiving text messages", "No - I do not consent"]
        self.assertEqual(DeterministicEngine.solve("Communication consent", sms_choices), "Yes - I consent to receiving text messages")

    def test_education_resolver_across_all_fields(self):
        """Verify Education fields resolve George Mason University / Master's / Information Systems."""
        self.assertEqual(DeterministicEngine.solve("School / University"), "George Mason University")
        self.assertEqual(DeterministicEngine.solve("Degree Level"), "Master's Degree")
        self.assertEqual(DeterministicEngine.solve("Field of Study / Major"), "Information Systems")
        self.assertEqual(DeterministicEngine.solve("Discipline"), "Information Systems")

    def test_canonical_answers_untouched(self):
        """Verify strict invariants for work auth, sponsorship, ITAR, and government employment."""
        # US Work Authorization -> Yes
        self.assertEqual(DeterministicEngine.solve("Are you legally authorized to work in the United States?"), "Yes")
        self.assertEqual(DeterministicEngine.solve("Legally Authorized to Work", ["Yes", "No"]), "Yes")

        # Visa Sponsorship -> Yes
        self.assertEqual(DeterministicEngine.solve("Will you now or in the future require visa sponsorship?"), "Yes")
        self.assertEqual(DeterministicEngine.solve("Require Visa Sponsorship", ["Yes", "No"]), "Yes")

        # ITAR / US Person Declaration -> Citizen of India, F-1 OPT
        itar_ans = DeterministicEngine.solve("U.S. Person status under ITAR / EAR export controls")
        self.assertIn("not a u.s. person", itar_ans.lower())
        self.assertIn("india", itar_ans.lower())

        # Government Employment -> No
        self.assertEqual(DeterministicEngine.solve("Have you ever been employed by the US federal government?"), "No")
        self.assertEqual(DeterministicEngine.solve("Government Employment", ["Yes", "No"]), "No")


class TestPreSubmitExtractionFailure(unittest.IsolatedAsyncioTestCase):
    """
    Validates Fix 1:
    Extraction failure returns EXTRACTION_FAILED, never resume_attached=False.
    """

    async def test_extraction_failure_returns_extraction_failed_never_resume_attached_false(self):
        """Ensure an extraction failure yields EXTRACTION_FAILED and NEVER reports 'Resume Attached is False'."""
        profile = load_profile()
        mock_page = AsyncMock()

        # Simulate extraction failure (e.g. Ashby evaluate SyntaxError or DOM failure)
        with patch("app.services.llm_verifier.extract_form_state") as mock_extract:
            mock_extract.return_value = {
                "fields": [],
                "resume_attached": None,  # INCONCLUSIVE / UNKNOWN, NEVER False
                "extraction_error": "SyntaxError: Invalid or unexpected token at evaluate"
            }

            approved, reason, issues = await llm_pre_submit_audit(
                mock_page,
                profile,
                screenshot_path="/tmp/test_extraction_failure.png"
            )

            # Verification must fail with EXTRACTION_FAILED
            self.assertFalse(approved)
            self.assertIn("EXTRACTION_FAILED", reason)
            self.assertIn("EXTRACTION_FAILED", issues)

            # Critical invariant: MUST NEVER claim "Resume Attached is False"
            self.assertNotIn("Resume Attached is False", reason)
            self.assertNotIn("Resume Attached is False", str(issues))
            self.assertNotIn("Resume is not attached", reason)
            self.assertNotIn("Resume is not attached", str(issues))

    async def test_inconclusive_resume_extraction_never_flags_false(self):
        """When resume_attached is None/Unknown, LLM filter never flags missing resume."""
        profile = load_profile()
        mock_page = AsyncMock()

        with patch("app.services.llm_verifier.extract_form_state") as mock_extract, \
             patch("app.services.llm_verifier.call_llm_json") as mock_llm:
            mock_extract.return_value = {
                "fields": [
                    {"label": "Full Name", "value": "Siddhesh Santosh Sawant", "required": True, "type": "text"},
                    {"label": "Email", "value": "siddheshs202001@gmail.com", "required": True, "type": "email"},
                    {"label": "Phone", "value": "(445) 208-2299", "required": True, "type": "tel"},
                ],
                "resume_attached": None,  # Inconclusive extraction
                "extraction_error": None
            }
            # LLM mistakenly tries to flag missing resume despite inconclusive extraction
            mock_llm.return_value = {
                "approved": False,
                "reason": "Resume is missing from the application",
                "issues": ["Resume is not attached to application"]
            }

            approved, reason, issues = await llm_pre_submit_audit(
                mock_page,
                profile,
                screenshot_path="/tmp/test_inconclusive_resume.png"
            )

            # Programmatic safety filter MUST suppress spurious resume flag when resume_attached is not False
            self.assertTrue(approved)
            self.assertEqual(issues, [])

    def test_date_field_and_text_field_get_identical_computed_date(self):
        """Date fields and text fields ('when can you start?') must receive the exact same date."""
        today = datetime.now()
        expected = compute_start_date(today)

        # 1. Date field representation
        date_field_val = resolve_start_date(fmt="%Y-%m-%d")
        # 2. Text field question representation
        text_field_val = DeterministicEngine.solve("When can you start a new role?")

        self.assertEqual(date_field_val, expected)
        self.assertEqual(text_field_val, expected)
        self.assertEqual(date_field_val, text_field_val)


if __name__ == "__main__":
    unittest.main()
