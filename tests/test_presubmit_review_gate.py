"""
Red-Green Harness for the PRESUBMIT_REVIEW Gate (Deterministic Checklist + Judge-Only LLM)
and Updated Nov 22 R9 Canonical Answers.
"""

import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from app.services.form_guards import FormHaltException
from form_filler import DeterministicEngine

TEST_HOME_ADDRESS = "123 Main St, Washington, DC 20001"
TEST_PROFILE = {"personal_info": {"home_address": TEST_HOME_ADDRESS}}


class TestPresubmitReviewGate(unittest.TestCase):
    """Mandatory review-gate tests (a)-(g) from Nov 22 Phase 2."""

    def _base_correct_fields(self):
        start_dt = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        return [
            {"label": "First Name", "value": "Siddhesh Santosh", "type": "text", "required": True},
            {"label": "Last Name", "value": "Sawant", "type": "text", "required": True},
            {"label": "Email", "value": "siddheshs202001@gmail.com", "type": "email", "required": True},
            {"label": "GitHub", "value": "https://github.com/sids202001", "type": "url", "required": True},
            {"label": "LinkedIn", "value": "https://www.linkedin.com/in/sss20/", "type": "url", "required": True},
            {"label": "Location", "value": "Washington, District of Columbia, United States", "type": "text", "required": True},
            {"label": "Home Address", "value": TEST_HOME_ADDRESS, "type": "text", "required": True},
            {"label": "Are you legally authorized to work in the United States?", "value": "Yes", "type": "radio", "required": True},
            {"label": "Will you now or in the future require visa sponsorship?", "value": "Yes", "type": "radio", "required": True},
            {"label": "Which visa type will you require?", "value": "H-1B", "type": "select", "required": True},
            {"label": "How did you hear about this opportunity?", "value": "Company website / careers page", "type": "radio", "required": True},
            {"label": "Are you willing to relocate?", "value": "Yes", "type": "radio", "required": True},
            {"label": "Are you located within 50 miles of Denver, CO?", "value": "No, but willing to relocate", "type": "radio", "required": True},
            {"label": "Expected Salary", "value": "100000", "type": "text", "required": True},
            {"label": "Available Start Date", "value": start_dt, "type": "date", "required": True},
            {"label": "Education 1 Start Month", "value": "January", "type": "select", "required": True},
            {"label": "Education 1 Start Year", "value": "2023", "type": "text", "required": True},
            {"label": "Education 1 End Month", "value": "May", "type": "select", "required": True},
            {"label": "Education 1 End Year", "value": "2024", "type": "text", "required": True},
            {"label": "Gender", "value": "Male", "type": "select", "required": True},
            {"label": "Are you a person of transgender experience?", "value": "No", "type": "select", "required": True},
            {"label": "Race / Ethnicity", "value": "Asian", "type": "select", "required": True},
            {"label": "Veteran Status", "value": "I am not a protected veteran", "type": "select", "required": True},
            {"label": "Disability Status", "value": "No, I do not have a disability", "type": "select", "required": True},
            {"label": "Sexual Orientation", "value": "Heterosexual", "type": "select", "required": True},
            {"label": "Languages Spoken", "value": "English, Marathi, Hindi", "type": "text", "required": False},
        ]

    def test_b_correct_form_passes_review_gate(self):
        from app.services.presubmit_review import run_presubmit_review
        result = run_presubmit_review(self._base_correct_fields(), profile=TEST_PROFILE)
        self.assertTrue(result["passed"], f"Expected PASS, got failures: {result['failures']}")
        self.assertEqual(result["decision"], "PASS")
        self.assertEqual(len(result["failures"]), 0)

    def test_a_planted_wrong_answers_blocked(self):
        from app.services.presubmit_review import run_presubmit_review
        planted_cases = [
            ("Which best describes your experience in travel or travel-adjacent services?", "I have worked directly at a travel or travel-adjacent company"),
            ("Have you applied to that same position in the last 3 months?", "Yes"),
            ("Are you currently a Twitch employee?", "Yes"),
            ("Are you a person of transgender experience?", "Yes"),
            ("What sexual orientation do you most closely identify with?", "Asexual"),
            ("How did you hear about this opportunity?", "LinkedIn"),
            ("GitHub URL", "https://github.com/wronguser"),
        ]
        for label, bad_val in planted_cases:
            fields = self._base_correct_fields()
            fields.append({"label": label, "value": bad_val, "type": "radio", "required": True})
            res = run_presubmit_review(fields)
            self.assertFalse(res["passed"], f"Gate MUST block planted wrong answer: {label}={bad_val}")
            self.assertEqual(res["halt_code"], "PRESUBMIT_REVIEW_FAILED")

    def test_c_blank_required_or_uncertain_halts(self):
        from app.services.presubmit_review import run_presubmit_review
        fields = self._base_correct_fields()
        fields.append({"label": "Are you authorized to work?", "value": "", "type": "radio", "required": True})
        res = run_presubmit_review(fields)
        self.assertFalse(res["passed"])
        self.assertEqual(res["halt_code"], "PRESUBMIT_REVIEW_FAILED")

    def test_d_raw_value_on_blocks(self):
        from app.services.presubmit_review import run_presubmit_review
        fields = self._base_correct_fields()
        fields.append({"label": "Are you willing to relocate?", "value": "on", "type": "radio", "required": True})
        res = run_presubmit_review(fields)
        self.assertFalse(res["passed"])
        self.assertEqual(res["halt_code"], "PRESUBMIT_REVIEW_FAILED")

    def test_e_eeo_value_in_wrong_field_blocks(self):
        from app.services.presubmit_review import run_presubmit_review
        # Veteran field with "Male" (Reddit leakage)
        fields = [f for f in self._base_correct_fields() if f["label"] != "Veteran Status"]
        fields.append({"label": "Veteran Status", "value": "Male", "type": "select", "required": True})
        res = run_presubmit_review(fields)
        self.assertFalse(res["passed"])
        self.assertEqual(res["halt_code"], "PRESUBMIT_REVIEW_FAILED")

        # Race field with "American Indian or Alaska Native"
        fields2 = [f for f in self._base_correct_fields() if f["label"] != "Race / Ethnicity"]
        fields2.append({"label": "Race / Ethnicity", "value": "American Indian or Alaska Native", "type": "select", "required": True})
        res2 = run_presubmit_review(fields2)
        self.assertFalse(res2["passed"])
        self.assertEqual(res2["halt_code"], "PRESUBMIT_REVIEW_FAILED")

    def test_f_education_year_blank_blocks(self):
        from app.services.presubmit_review import run_presubmit_review
        fields = [f for f in self._base_correct_fields() if f["label"] != "Education 1 End Year"]
        fields.append({"label": "Education 1 End Year", "value": "", "type": "text", "required": False})
        res = run_presubmit_review(fields)
        self.assertFalse(res["passed"])
        self.assertEqual(res["halt_code"], "PRESUBMIT_REVIEW_FAILED")

    def test_g_generated_prose_in_freetext_blocks(self):
        from app.services.presubmit_review import run_presubmit_review
        fields = self._base_correct_fields()
        fields.append({
            "label": "Where did you gain your GCP experience?",
            "value": "Yes, having worked as both a Software Engineer and Business Analyst, I excel at collaborating with product managers and stakeholders.",
            "type": "textarea",
            "required": True,
        })
        res = run_presubmit_review(fields)
        self.assertFalse(res["passed"])
        self.assertEqual(res["halt_code"], "PRESUBMIT_REVIEW_FAILED")


class TestUpdatedR9CanonicalAnswers(unittest.TestCase):
    """User-confirmed Nov 22 R9 answers."""

    def test_home_address_now_canonical(self):
        with patch("form_filler.load_profile", return_value=TEST_PROFILE):
            ans = DeterministicEngine.solve("Home Street Address", None, is_required=True)
        self.assertEqual(ans, TEST_HOME_ADDRESS)

    def test_travel_30_50_now_confirmed_yes(self):
        q = "This role involves 30 to 50 percent travel, including multi-week stretches on site with global partners. Does this work for you?"
        self.assertEqual(DeterministicEngine.solve(q, ["Yes", "No"]), "Yes")

    def test_amazon_employment_history_and_current_employee(self):
        self.assertEqual(DeterministicEngine.solve("Have you previously been employed by Amazon or any Amazon subsidiary?", ["Yes", "No"]), "Yes")
        self.assertEqual(DeterministicEngine.solve("Are you a current employee with Amazon or any Amazon subsidiary?", ["Yes", "No"]), "No")
        self.assertEqual(DeterministicEngine.solve("Have you previously applied to Amazon or any Amazon subsidiary?", ["Yes", "No"]), "Yes")

    def test_may_we_contact_current_employer_yes(self):
        self.assertEqual(DeterministicEngine.solve("May we contact your current employer?", ["Yes", "No"]), "Yes")

    def test_sexual_orientation_heterosexual_and_transgender_no(self):
        self.assertEqual(
            DeterministicEngine.solve("What sexual orientation do you most closely identify with?", ["Asexual", "Bisexual", "Heterosexual", "Prefer not to say"]),
            "Heterosexual"
        )
        self.assertEqual(
            DeterministicEngine.solve("Are you a person of transgender experience?", ["Yes", "No", "I don't wish to answer"]),
            "No"
        )

    def test_authorization_expiration_date_07_08_2027(self):
        self.assertEqual(
            DeterministicEngine.solve("If you have a current work authorization expiration date, please enter it:"),
            "07/08/2027"
        )

    def test_bachelors_completion_aug_2022_and_ny_ca_options101_no(self):
        self.assertEqual(
            DeterministicEngine.solve("When did you complete, or when do you expect to complete, your Bachelor's degree?", ["2022", "2023", "2024", "2025", "2026"]),
            "2022"
        )
        self.assertEqual(DeterministicEngine.solve("Do you currently live in New York or California?", ["Yes", "No"]), "No")
        self.assertEqual(DeterministicEngine.solve("Have you completed the Akuna Options 101 course?", ["Yes", "No"]), "No")
        self.assertEqual(DeterministicEngine.solve("Do you have prior options market-making firm experience?", ["Yes", "No"]), "No")

    def test_remote_work_and_relocation_assistance_canonical_nov22(self):
        self.assertEqual(DeterministicEngine.solve("Are you willing to work remotely?", ["Yes", "No"]), "Yes")
        self.assertEqual(DeterministicEngine.solve("Do you require relocation assistance?", ["Yes", "No"]), "No")
        self.assertEqual(DeterministicEngine.solve("Are you open to relocation assistance if the company provides it?", ["Yes", "No"]), "Yes")


class TestGroundedWhyCompanyEssays(unittest.TestCase):
    """Tests for Grounded 'Why [Company]?' essay writing & presubmit review verification (Rules 1-5)."""

    GOALY_JD = (
        "ABOUT US\n"
        "Goaly is an early-stage AI startup founded by ex-Meta Superintelligence Labs engineers and researchers. "
        "Goaly is the autonomous performance layer for AI training. It watches every job across a GPU fleet, catches "
        "the runs that are wasting compute, reallocates what it frees up, and shows teams what each model improvement "
        "actually costs. It runs on-prem, telemetry-only, and starts read-only.\n"
        "ABOUT THE ROLE\n"
        "You will be one of our first Forward Deployed Engineers, working directly with the founding team, "
        "engineering, and GTM to own the technical customer journey, bridge customers and product, translate customer "
        "needs into clear, actionable requirements for engineering, and build lightweight solutions in Python."
    )
    ABRIDGE_ABOUT = (
        "About us — Built for enterprise healthcare. Abridge is the first AI-native clinician intelligence platform, "
        "designed to help health systems coordinate the clinical, financial, and evidence-based decisions for every "
        "moment of care across more than 300 of the largest and most complex health systems in the U.S."
    )
    ABRIDGE_JD = (
        "ABOUT ABRIDGE\n"
        + ABRIDGE_ABOUT
        + "\nTHE ROLE\n"
        "We're looking for a Business Systems Engineer to connect and automate the systems Abridge runs on, starting "
        "with Finance. Keep every finance system flowing into Snowflake, accurately and on time: ERP, spend "
        "management, banking, CRM, HRIS and payroll, and cloud and AI cost. Help accountants and analysts build, ship "
        "and maintain their own agentic workflows for close, reconciliations, AP, procurement, revenue recognition, "
        "technical accounting and reporting. You'll also know when a problem needs an agent and when a plain, "
        "deterministic pipeline is the better answer."
    )

    def tearDown(self):
        from app.services.grounded_essay import clear_application_context
        clear_application_context()

    def test_1_why_company_question_detection_vs_non_why_company(self):
        from app.services.grounded_essay import is_why_company_question
        for q in [
            "Why Goaly",
            "Why Goaly?",
            "Why are you interested in working at Abridge?",
            "Why do you want to work at Abridge?",
            "Why join Goaly?",
        ]:
            self.assertTrue(is_why_company_question(q), f"Expected True for: {q}")
        for q in [
            "Why are you a good fit for this role at Anthropic?",
            "Why should we hire you?",
            "Why this role?",
            "Is there anything else you'd like us to know?",
            "Describe your proudest engineering accomplishment.",
            "Where did you gain your GCP experience?",
        ]:
            self.assertFalse(is_why_company_question(q), f"Expected False for: {q}")

    def test_2_writes_grounded_2_to_4_sentence_essay_and_passes_presubmit_gate(self):
        from app.services.grounded_essay import set_application_context, split_sentences
        from app.services.presubmit_review import run_presubmit_review

        for co, title, q, jd, about in [
            ("Goaly", "Founding Forward Deployed Engineer (FDE)", "Why Goaly", self.GOALY_JD, ""),
            ("Abridge", "Business Systems Engineer - Finance", "Why are you interested in working at Abridge?", self.ABRIDGE_JD, self.ABRIDGE_ABOUT),
        ]:
            set_application_context(company=co, title=title, url=f"offline://{co.lower()}", jd_text=jd, about_text=about)
            ans = DeterministicEngine.solve(q, None, is_required=True)
            self.assertIsInstance(ans, str)
            sents = split_sentences(ans)
            self.assertTrue(2 <= len(sents) <= 4, f"Expected 2-4 sentences for {co}, got {len(sents)}: {ans}")
            res = run_presubmit_review([{"label": q, "value": ans, "type": "textarea", "required": True}])
            self.assertTrue(res["passed"], f"Expected PRESUBMIT_REVIEW PASS for {co}, got: {res['failures']}")

    def test_3_presubmit_gate_fails_on_generic_praise_unowned_skills_or_false_company_claims(self):
        from app.services.grounded_essay import set_application_context
        from app.services.presubmit_review import run_presubmit_review

        set_application_context(
            company="Abridge",
            title="Business Systems Engineer - Finance",
            url="offline://abridge",
            jd_text=self.ABRIDGE_JD,
            about_text=self.ABRIDGE_ABOUT,
        )
        bad_essays = [
            # Generic praise ("passionate about your mission", "innovative culture")
            "I am passionate about your mission and excited to join your innovative culture at Abridge. "
            "At Amazon Web Services, I built Python and SQL data pipelines that would make an immediate positive impact.",
            # Skill/experience he does not have (Kubernetes, Rust, GCP, Google)
            "Abridge is the first AI-native clinician intelligence platform keeping every finance system flowing into Snowflake. "
            "At Google, I architected Kubernetes and Rust microservices on GCP for enterprise financial systems.",
            # Claim about company not in JD/about page (autonomous drone delivery & blockchain)
            "Abridge is building an autonomous drone delivery network and blockchain cryptocurrency exchange. "
            "At Amazon Web Services, I built automated validation frameworks using AWS Step Functions, Batch, and S3.",
        ]
        for bad in bad_essays:
            res = run_presubmit_review([
                {"label": "Why are you interested in working at Abridge?", "value": bad, "type": "textarea", "required": True}
            ])
            self.assertFalse(res["passed"], f"Presubmit gate MUST fail on ungrounded essay: {bad}")
            self.assertEqual(res["halt_code"], "PRESUBMIT_REVIEW_FAILED")

    def test_4_other_freetext_fields_still_halt_and_fail_presubmit_on_prose(self):
        from app.services.presubmit_review import run_presubmit_review
        # Other free-text fields ("anything else", fit, experience questions) still halt in DeterministicEngine
        with self.assertRaises(FormHaltException) as ctx:
            DeterministicEngine.solve("Is there anything else you'd like us to know?", None, is_required=True)
        self.assertEqual(ctx.exception.code, "ESSAY_HALT")

        # And still FAIL in presubmit review if generated prose is present
        res = run_presubmit_review([{
            "label": "Is there anything else you'd like us to know?",
            "value": "At Amazon Web Services, I designed an automated validation framework using AWS Step Functions and S3 that eliminated race conditions in AWS Batch.",
            "type": "textarea",
            "required": False,
        }])
        self.assertFalse(res["passed"])
        self.assertEqual(res["halt_code"], "PRESUBMIT_REVIEW_FAILED")

    def test_5_halts_when_cannot_ground_missing_jd_or_no_concrete_link(self):
        from app.services.grounded_essay import clear_application_context, set_application_context
        # 5a. No JD/about page readable -> halts and parks
        clear_application_context()
        with self.assertRaises(FormHaltException) as ctx:
            DeterministicEngine.solve("Why Goaly", None, is_required=True)
        self.assertEqual(ctx.exception.code, "ESSAY_HALT")

        # 5b. Unrelated JD/about page with no concrete link to his software/data/AI work -> halts and parks
        unrelated_about = (
            "ABOUT US\n"
            "PetalFlora Studio is a traditional floral arrangement boutique specializing in hand-tied bridal bouquets, "
            "fresh cut peonies, orchids, botanical table centerpieces, and seasonal greenhouse floristry."
        )
        unrelated_jd = (
            unrelated_about
            + "\nTHE ROLE\n"
            "Seeking an onsite Lead Floral Arranger to trim stems, hydrate roses, condition lilies, arrange wedding "
            "bouquets, wrap floral deliveries, and maintain cold-storage flower coolers."
        )
        set_application_context(
            company="PetalFlora",
            title="Lead Floral Arranger",
            url="offline://petalflora",
            jd_text=unrelated_jd,
            about_text=unrelated_about,
        )
        with self.assertRaises(FormHaltException) as ctx2:
            DeterministicEngine.solve("Why PetalFlora?", None, is_required=True)
        self.assertEqual(ctx2.exception.code, "ESSAY_HALT")

    def test_6_generalized_open_text_experience_project_bug_and_fit_questions(self):
        """Requirement 1 & 2: Generalized open-text writer handles experience, project, bug, and role-fit questions."""
        from app.services.grounded_essay import clear_application_context, set_application_context, split_sentences
        from app.services.presubmit_review import run_presubmit_review

        clear_application_context()
        candidate_only_questions = [
            "Describe your experience in AI and Python.",
            "Describe your experience with AWS and SQL data pipelines.",
            "Describe a project you are proud of.",
            "Tell us about a challenging project you built.",
            "Describe a bug you fixed in production.",
        ]
        for q in candidate_only_questions:
            ans = DeterministicEngine.solve(q, None, is_required=True)
            self.assertTrue(isinstance(ans, str) and len(ans) > 60, f"Expected grounded answer for '{q}'")
            sents = split_sentences(ans)
            self.assertTrue(2 <= len(sents) <= 4, f"Expected 2-4 sentences for '{q}', got {len(sents)}: {ans}")
            res = run_presubmit_review([{"label": q, "value": ans, "type": "textarea", "required": True}])
            self.assertTrue(res["passed"], f"Expected PRESUBMIT_REVIEW PASS for '{q}', got: {res['failures']}")

        # Role-fit & 1-2 sentence company/role question with JD context
        set_application_context(
            company="Abridge",
            title="Business Systems Engineer - Finance",
            url="offline://abridge",
            jd_text=self.ABRIDGE_JD,
            about_text=self.ABRIDGE_ABOUT,
        )
        for q_role in [
            "Why are you a good fit for this role?",
            "What makes you a strong candidate?",
            "In one or two sentences, why Abridge and why this role?",
        ]:
            ans = DeterministicEngine.solve(q_role, None, is_required=True)
            self.assertTrue(isinstance(ans, str) and len(ans) > 60)
            res = run_presubmit_review([{"label": q_role, "value": ans, "type": "textarea", "required": True}])
            self.assertTrue(res["passed"], f"Expected PRESUBMIT_REVIEW PASS for '{q_role}', got: {res['failures']}")

    def test_7_achievement_and_metric_grounding_rejects_tool_only_false_achievements(self):
        """Requirement 3: A tool or skill name alone NEVER grounds an achievement or metric claim."""
        from app.services.grounded_essay import clear_application_context
        from app.services.presubmit_review import run_presubmit_review

        clear_application_context()
        false_achievement_answers = [
            # Real tools (AWS, Python, CloudWatch) but false metric ("from days to seconds", "80%")
            "At Amazon Web Services, I used Python and CloudWatch to reduce API latency from days to seconds by 80%. "
            "I also built Autonomous AI Job Scout in Python, FastAPI, and Playwright.",
            # Real tools (AWS, Python, Lambda, S3) but false achievement ("built a payment gateway and database sharding engine")
            "At Amazon Web Services, I built a Python payment gateway and database sharding engine using Lambda and S3. "
            "I also built Autonomous AI Job Scout in Python, FastAPI, and Playwright.",
        ]
        for bad_ans in false_achievement_answers:
            res = run_presubmit_review([{
                "label": "Describe your experience in AI and Python.",
                "value": bad_ans,
                "type": "textarea",
                "required": True,
            }])
            self.assertFalse(res["passed"], f"Gate MUST fail on ungrounded achievement/metric: {bad_ans}")
            self.assertEqual(res["halt_code"], "PRESUBMIT_REVIEW_FAILED")

    def test_8_hard_limits_halt_on_ratings_age_commitments_legal_and_unowned_skills(self):
        """Requirement 4: Hard limits still HALT (never guess, never write)."""
        from app.services.grounded_essay import clear_application_context
        clear_application_context()

        # 4a. "Is there anything else?" -> blank when optional, halt when required
        self.assertEqual(DeterministicEngine.solve("Anything else to add?", None, is_required=False), "")
        with self.assertRaises(FormHaltException):
            DeterministicEngine.solve("Anything else to add?", None, is_required=True)

        # 4b-4f. Self-ratings, age/DOB, weekend/pay-cut/night-shift decisions, legal license, unowned skills
        hard_limit_prompts = [
            "Rate your Spanish proficiency on a scale of 1 to 10.",
            "What is your date of birth?",
            "Are you willing to work weekends?",
            "Will you accept a pay cut for equity?",
            "Will you work night shifts?",
            "Please provide your state bar admission number.",
            "Describe your experience with Kubernetes and Rust.",
        ]
        for q in hard_limit_prompts:
            with self.assertRaises(FormHaltException, msg=f"Expected FormHaltException for hard limit: {q}"):
                DeterministicEngine.solve(q, None, is_required=True)

    def test_9_boilerplate_salary_text_never_copied_into_company_answers(self):
        """HR/salary/EEO boilerplate must never be selected as a company clause or pass verification."""
        from app.services.grounded_essay import (
            compose_deterministic_grounded_essay,
            rank_candidate_facts_for_sources,
            verify_grounded_essay,
        )

        sources = {
            "company": "WHOOP",
            "title": "Software Engineer II (Fullstack, Business Systems)",
            "jd_text": (
                "Salary ranges are determined by role, level, and location.\n"
                "WHOOP builds wearable health performance hardware and software systems.\n"
                "You will develop, test, and deploy scalable Python software solutions and data pipelines "
                "supporting internal supply chain and business system integrations."
            ),
            "about_text": (
                "Salary ranges are determined by role, level, and location.\n"
                "WHOOP builds wearable health performance hardware and software systems for continuous physiological monitoring."
            ),
        }
        ranked = rank_candidate_facts_for_sources(sources)
        composed = compose_deterministic_grounded_essay(sources, ranked, question="Why WHOOP?")
        ans, claims = composed["answer"], composed["claims"]
        self.assertTrue(ans, "Expected a grounded answer excluding salary boilerplate")
        self.assertNotIn("salary range", ans.lower())
        ok, errs = verify_grounded_essay(ans, claims, sources, question="Why WHOOP?")
        self.assertTrue(ok, f"Expected clean grounded answer to pass, got: {errs}")

        bad_ans = "Salary ranges are determined by role, level, and location. " + ans
        ok_bad, errs_bad = verify_grounded_essay(bad_ans, claims, sources, question="Why WHOOP?")
        self.assertFalse(ok_bad, "Expected boilerplate-prefixed answer to FAIL verification")

    def test_10_visa_support_and_sponsorship_phrasings_always_yes(self):
        """Any visa-support / sponsorship phrasing maps to canonical Yes and passes presubmit review."""
        from app.services.presubmit_review import run_presubmit_review
        from app.services.universal_registry import UniversalQuestionSolver

        phrasings = [
            "Visa Support",
            "Visa Support?",
            "Do you require visa support?",
            "Visa Assistance",
            "Immigration support",
            "Do you require visa sponsorship?",
            "Will you need visa sponsorship now or in the future?",
            "Will you require employment sponsorship?",
            "Sponsorship",
        ]
        solver = UniversalQuestionSolver()
        for q in phrasings:
            ans = DeterministicEngine.solve(q, ["Yes", "No"], is_required=True)
            self.assertEqual(ans, "Yes", f"DeterministicEngine.solve failed for '{q}': got {ans!r}")

            u_ans, _, u_stage = solver.solve(q, ["Yes", "No"], is_required=True)
            self.assertEqual(u_ans, "Yes", f"UniversalQuestionSolver failed for '{q}': {(u_ans, u_stage)}")

            rev_ok = run_presubmit_review([{"label": q, "value": "Yes", "type": "radio", "required": True}])
            self.assertTrue(rev_ok["passed"], f"Presubmit review should PASS Yes for '{q}': {rev_ok['failures']}")

            rev_bad = run_presubmit_review([{"label": q, "value": "No", "type": "radio", "required": True}])
            self.assertFalse(rev_bad["passed"], f"Presubmit review should FAIL No for '{q}'")

        # Ensure "Which visa?" is still H-1B (not hijacked by Yes)
        self.assertEqual(DeterministicEngine.solve("Which visa?", ["H-1B", "TN", "O-1"], is_required=True), "H-1B")


if __name__ == "__main__":
    unittest.main()
