import unittest
from form_filler import (
    score_option_match,
    DeterministicEngine,
    load_profile,
)
from app.services.universal_registry import UniversalQuestionSolver


class TestOptionSelectionHardening(unittest.TestCase):
    """
    Dedicated tests verifying the root-cause fixes:
    1. Negation rejection (Hadrian)
    2. Country work-auth vs residence disambiguation (CYVL)
    3. City-aware commute logic (DDN)
    4. Reverse-phrased previous employment detection (Ibotta)
    5. Demographic semantic synonym clusters (Male vs Man)
    6. Form completeness on previously missed selection fields
    """

    def test_hadrian_work_auth_negation_rejection(self):
        """Proof that 'not authorized' options receive 0.0 score against positive targets and are never selected."""
        opts = [
            "I am authorized to work in the United States for any employer",
            "I am authorized to work in the United States, but will require sponsorship in the future",
            "I am not authorized to work in the United States"
        ]

        # 1. Option scoring polarity check
        score_not_auth = score_option_match("Yes", "I am not authorized to work in the United States")
        self.assertEqual(score_not_auth, 0.0, "Negated authorization option must score 0.0 against 'Yes'")

        score_auth = score_option_match("Yes", "I am authorized to work in the United States, but will require sponsorship in the future")
        self.assertGreater(score_auth, 0.80, "Authorized option must score high against 'Yes'")

        # 2. DeterministicEngine resolution check
        q = "Are you legally authorized to work in the United States?"
        resolved = DeterministicEngine.solve(q, opts)
        self.assertIn("authorized", resolved.lower())
        self.assertNotIn("not authorized", resolved.lower())
        self.assertEqual(resolved, "I am authorized to work in the United States, but will require sponsorship in the future")

    def test_cyvl_country_work_auth_disambiguation(self):
        """Proof that country-phrased work auth questions resolve to 'Yes' and are not hijacked by country residence."""
        q_auth = "Are you legally authorized to work in the country where this job is located?"
        ans_auth = DeterministicEngine.solve(q_auth, ["Yes", "No"])
        self.assertEqual(ans_auth, "Yes", "Country-scoped work auth must resolve to 'Yes'")

        q_spons = "Do you now, or will you ever, require employment sponsorship to work in the country where this job is located?"
        ans_spons = DeterministicEngine.solve(q_spons, ["Yes", "No"])
        self.assertEqual(ans_spons, "Yes", "Country-scoped sponsorship must resolve to 'Yes'")

        # Verify universal registry does NOT match 'country' to contact info
        solver = UniversalQuestionSolver()
        res_auth, prov_auth, _ = solver.solve(q_auth, choices=["Yes", "No"])
        self.assertNotEqual(res_auth, "United States", "Work authorization question must not resolve to literal 'United States'")
        self.assertEqual(res_auth, "Yes")

    def test_ddn_commute_city_awareness(self):
        """Proof of commute city awareness and truthful DC vs non-DC resolution (Rule 4 Addendum)."""
        # 1. Non-DC commuting distance with Yes/No choices answers "No"
        ans_cary = DeterministicEngine.solve("Are you located within a commuting distance to Cary, NC?", ["Yes", "No"])
        self.assertEqual(ans_cary, "No")

        ans_denver = DeterministicEngine.solve("Are you able to commute daily to our Denver, CO office?", ["Yes", "No"])
        self.assertEqual(ans_denver, "No")

        # 2. DC commuting distance with Yes/No choices answers "Yes"
        ans_dc = DeterministicEngine.solve("Are you located within commuting distance to Washington, DC?", ["Yes", "No"])
        self.assertEqual(ans_dc, "Yes")

        # 3. Willingness to work in hybrid model and commute answers "Yes" from profile.json
        ans_hybrid = DeterministicEngine.solve("Are you willing to work in a hybrid model and commute to the local office?", ["Yes", "No"])
        self.assertEqual(ans_hybrid, "Yes")

    def test_ibotta_reverse_phrased_previous_employment(self):
        """Proof that reverse-phrased 'worked for [Company] previously' resolves to 'No'."""
        q_ibotta = "Have you worked for Ibotta previously?"
        ans_ibotta = DeterministicEngine.solve(q_ibotta, ["Yes", "No"])
        self.assertEqual(ans_ibotta, "No", "Previous employment with Ibotta must resolve to 'No'")

        q_other = "Have you worked at Stripe previously?"
        ans_other = DeterministicEngine.solve(q_other, ["Yes", "No"])
        self.assertEqual(ans_other, "No", "Previous employment with target company must resolve to 'No'")

        q_former = "Are you a former employee or contractor of this company?"
        ans_former = DeterministicEngine.solve(q_former, ["Yes", "No"])
        self.assertEqual(ans_former, "No", "Former employee check must resolve to 'No'")

    def test_demographic_synonym_clusters(self):
        """Proof that semantic synonym clusters allow 'Male' to match 'Man' while rejecting opposite gender."""
        score_male_man = score_option_match("Male", "Man")
        self.assertGreaterEqual(score_male_man, 0.90, "Male must match Man via synonym cluster")

        score_male_woman = score_option_match("Male", "Woman")
        self.assertEqual(score_male_woman, 0.0, "Male must score 0.0 against Woman")

        score_ft = score_option_match("Full-time", "Full time")
        self.assertGreaterEqual(score_ft, 0.90, "Full-time must match Full time")

    def test_previously_missing_selection_questions(self):
        """Proof that radio/toggle questions previously left blank now resolve deterministically."""
        checks = [
            ("Are you 18 years of age or older?", ["Yes", "No"], "Yes"),
            ("Are you at least 18 years of age?", ["Yes", "No"], "Yes"),
            ("Do you have any conflicts of interest?", ["Yes", "No"], "No"),
            ("Are you legally eligible to work in the United States?", ["Yes", "No"], "Yes"),
            ("Will you now or in the future require visa sponsorship for employment?", ["Yes", "No"], "Yes"),
            ("Will you require sponsorship for an employment visa (e.g. H-1B, OPT)?", ["Yes", "No"], "Yes"),
            ("Are you willing to undergo a background check?", ["Yes", "No"], "Yes"),
            ("Will you now or in the future require company sponsorship for an immigration-related employment status?", ["Yes", "No"], "Yes"),
        ]
        for q, choices, expected in checks:
            ans = DeterministicEngine.solve(q, choices)
            self.assertEqual(ans, expected, f"Question '{q}' expected '{expected}', got '{ans}'")


    def test_post_submit_verification_affirmative_patterns(self):
        """Proof that Greenhouse embed confirmation phrasing matches CONFIRMATION_TEXT_PATTERNS."""
        from app.services.llm_verifier import CONFIRMATION_TEXT_PATTERNS
        from app.services.auto_filler import CONFIRMATION_TEXT_PATTERNS as AUTO_CONFIRM_PATTERNS
        from form_filler import verify_submission

        greenhouse_text = (
            "thank you for your interest in parallel systems. we wanted to let you know "
            "we received your application, and we are delighted that you would consider joining our team. "
            "track your application: initial screen -> team interview -> onsite interview -> offer"
        )
        matched_patterns = [p for p in CONFIRMATION_TEXT_PATTERNS if p in greenhouse_text]
        self.assertIn("we received your application", matched_patterns)
        self.assertIn("thank you for your interest", matched_patterns)
        self.assertIn("track your application", matched_patterns)

        # Ensure auto_filler patterns are synchronized
        self.assertIn("we received your application", AUTO_CONFIRM_PATTERNS)
        self.assertIn("track your application", AUTO_CONFIRM_PATTERNS)

    def test_digest_includes_verified_submissions_consistently(self):
        """Proof that audit_logger digest displays verified terminal submissions from launch_tracker."""
        from app.services.audit_logger import audit_logger
        from app.services.launch_tracker import launch_tracker

        audit_logger.reset_run()
        minimal = audit_logger.generate_minimal_digest()
        subs = [l for l in launch_tracker.get_last_24h_launches() if l.get("status") == "SUBMITTED"]
        if subs:
            self.assertIn(subs[0]["company"], minimal)
            self.assertIn(subs[0]["role"], minimal)
        else:
            audit_logger._current_run_submitted.append({"company": "TestComp", "role": "TestRole", "receipt_msg_id": "999"})
            min_test = audit_logger.generate_minimal_digest()
            self.assertIn("TestComp", min_test)

        full = audit_logger.generate_run_digest()
        self.assertIn("*Submitted Applications:*", full)


if __name__ == "__main__":
    unittest.main()

