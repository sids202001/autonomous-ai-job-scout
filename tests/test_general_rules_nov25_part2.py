"""
Regression tests for general screening rules (Nov 25 Part 2):
1. Conditional follow-up fields (Datalabusa referral name, termination follow-up, and ANY false-condition follow-up):
   - Optional (no *) -> skipped / left blank (never halts)
   - Required (with *) -> filled with "N/A" (never halts)
   - Context-aware parent-answer follow-up: "Have you ever been terminated?" -> "No" followed by "Please tell company name" -> "N/A" (req) / "" (opt)
   - Positive conditional follow-up (e.g., sponsorship details when sponsorship=Yes) -> real profile detail, NOT N/A
2. General education & school-specific enrollment/attendance rule (CodeRabbit UPenn & ANY university/program):
   - Currently enrolled / about to graduate / currently pursuing degree -> No
   - School-specific questions for any university NOT in profile.json['education'] (UPenn, Stanford, MIT, CMU, Berkeley, Waterloo, etc.) -> No
   - Past attendance/graduation for schools IN profile.json['education'] (George Mason University / GMU, University of Mumbai) -> Yes
   - Non-yes/no school questions ("What university did you attend?") still return "George Mason University"
3. Skip non-required unknown fields across ALL portals (Lever, Ashby, Greenhouse):
   - Optional unknown text/radio/select/toggle -> skipped & left blank (no halt, no guessing)
   - Required unknown field -> still halts with UNMAPPED_SCREENING_QUESTION (no guessing)
4. PRESUBMIT_REVIEW gate compatibility:
   - Optional blank fields (including optional toggles) -> PASS
   - Required conditional follow-up filled with "N/A" (stateless and parent-context) -> PASS
   - School-specific enrollment answered "No" -> PASS
"""

import unittest
from playwright.async_api import async_playwright

from app.services.form_guards import (
    FormHaltException,
    is_conditional_followup_question,
    solve_conditional_followup,
    solve_education_enrollment_question,
    solve_canonical_screening_question,
)
from app.services.presubmit_review import run_presubmit_review
from form_filler import (
    DeterministicEngine,
    GeminiSolver,
    UnifiedATSFormFiller,
    load_profile,
)


class TestGeneralConditionalAndEducationRules(unittest.TestCase):
    def setUp(self):
        self.ai = GeminiSolver()

    # --- 1. Conditional follow-ups (Datalabusa + general) ---
    def test_datalabusa_referral_name_optional_skips_and_required_na(self):
        q = "If you are a Referral, please mention their name."
        self.assertTrue(is_conditional_followup_question(q))
        # Optional -> blank (skip), never halts
        self.assertEqual(DeterministicEngine.solve(q, None, is_required=False), "")
        self.assertEqual(self.ai.solve(q, None, "text", is_required=False), "")
        # Required -> N/A, never halts
        self.assertEqual(DeterministicEngine.solve(q, None, is_required=True), "N/A")
        self.assertEqual(self.ai.solve(q, None, "text", is_required=True), "N/A")

    def test_general_referral_and_false_condition_phrasings_any_company(self):
        prompts = [
            "If you were referred by an employee, please enter their name",
            "Employee referral name",
            "Who referred you?",
            "Name of the referring employee",
            "If you are a veteran, please specify your branch and discharge status",
            "If yes, please describe the accommodation needed",
            "If you have ever been terminated, please provide the company name",
            "If yes, please explain",
            "If applicable, please list dates",
            "If you chose Other, please specify",
        ]
        for q in prompts:
            self.assertTrue(is_conditional_followup_question(q), f"Expected conditional follow-up: {q}")
            self.assertEqual(DeterministicEngine.solve(q, None, is_required=False), "", f"Optional should be blank for: {q}")
            self.assertEqual(DeterministicEngine.solve(q, None, is_required=True), "N/A", f"Required should be N/A for: {q}")

    def test_parent_context_terminated_followup_company_name(self):
        parent_q = "Have you ever been terminated?"
        parent_a = DeterministicEngine.solve(parent_q, ["Yes", "No"], is_required=True)
        self.assertEqual(parent_a, "No")

        followup_q = "Please tell company name"
        self.assertTrue(
            is_conditional_followup_question(followup_q, parent_question=parent_q, parent_answer=parent_a)
        )
        self.assertEqual(
            DeterministicEngine.solve(
                followup_q, None, is_required=True, parent_question=parent_q, parent_answer=parent_a
            ),
            "N/A",
        )
        self.assertEqual(
            DeterministicEngine.solve(
                followup_q, None, is_required=False, parent_question=parent_q, parent_answer=parent_a
            ),
            "",
        )

    def test_positive_conditionals_not_clobbered_by_na(self):
        # Work auth expiration stays 07/08/2027
        self.assertEqual(
            DeterministicEngine.solve("If you have a current work authorization expiration date, please enter it:", None, is_required=True),
            "07/08/2027",
        )
        # Sponsorship follow-up (parent is Yes) returns real sponsorship detail, NOT N/A
        spons_detail = DeterministicEngine.solve(
            "If yes, please specify your visa sponsorship requirements", None, is_required=True
        )
        self.assertIn("OPT", spons_detail)
        self.assertNotEqual(spons_detail, "N/A")
        # Core standalone "Current Company" is NOT turned into N/A even after a "No" parent
        self.assertEqual(
            DeterministicEngine.solve(
                "Current Company", None, is_required=True,
                parent_question="Have you ever been terminated?", parent_answer="No"
            ),
            "Talviro INC",
        )

    # --- 2. School-specific & current-enrollment education rules (CodeRabbit + ANY university) ---
    def test_coderabbit_upenn_engineering_program_is_no(self):
        q = "Are you currently enrolled at, or about to graduate from, a UPenn Engineering Program?"
        self.assertEqual(solve_education_enrollment_question(q, ["Yes", "No"]), "No")
        self.assertEqual(DeterministicEngine.solve(q, ["Yes", "No"], is_required=True), "No")
        self.assertEqual(self.ai.solve(q, ["Yes", "No"], "radio", is_required=True), "No")

    def test_general_school_specific_questions_any_university(self):
        # Schools NOT in Siddhesh's education record -> No
        non_owned_questions = [
            "Are you currently enrolled at Stanford University?",
            "Did you graduate from MIT?",
            "Did you attend Carnegie Mellon University?",
            "Are you an alumnus of UC Berkeley?",
            "Have you attended a University of Pennsylvania engineering program?",
            "Are you currently pursuing a degree?",
            "Are you a current student?",
            "Are you about to graduate from a Computer Science program?",
            "Are you expecting to graduate in 2027?",
            "Did you attend Waterloo?",
        ]
        for q in non_owned_questions:
            self.assertEqual(
                DeterministicEngine.solve(q, ["Yes", "No"], is_required=True),
                "No",
                f"Expected No for: {q}",
            )

        # Schools IN Siddhesh's education record (past attendance/graduation) -> Yes
        owned_past_questions = [
            "Did you attend George Mason University?",
            "Did you graduate from George Mason University?",
            "Are you an alumnus of GMU?",
            "Did you graduate from the University of Mumbai?",
        ]
        for q in owned_past_questions:
            self.assertEqual(
                DeterministicEngine.solve(q, ["Yes", "No"], is_required=True),
                "Yes",
                f"Expected Yes for: {q}",
            )

        # Currently enrolled at GMU -> No (graduated May 2024, is_current=false)
        self.assertEqual(
            DeterministicEngine.solve("Are you currently enrolled at George Mason University?", ["Yes", "No"], is_required=True),
            "No",
        )

        # Non-yes/no school prompt still returns George Mason University
        self.assertEqual(
            DeterministicEngine.solve("What university did you attend?", None, is_required=True),
            "George Mason University",
        )

    # --- 3. Non-required unknown fields skip, required unknown fields still halt ---
    def test_optional_unknown_skips_and_required_unknown_still_halts(self):
        unknown_q = "Do you hold an active FAA commercial pilot license?"
        # Optional -> returns "" and never halts
        self.assertIsNone(DeterministicEngine.solve(unknown_q, ["Yes", "No"], is_required=False))
        self.assertEqual(self.ai.solve(unknown_q, ["Yes", "No"], "radio", is_required=False), "")

        # Required -> still halts (standing no-guessing rule preserved)
        with self.assertRaises(FormHaltException) as ctx:
            self.ai.solve(unknown_q, ["Yes", "No"], "radio", is_required=True)
        self.assertEqual(ctx.exception.code, "UNMAPPED_SCREENING_QUESTION")

    # --- 4. PRESUBMIT_REVIEW compatibility ---
    def test_presubmit_review_passes_conditional_na_upenn_no_and_optional_blank_toggle(self):
        fields = [
            {"label": "Have you ever been terminated?", "value": "No", "type": "radio", "required": True},
            {"label": "Please tell company name", "value": "N/A", "type": "text", "required": True},
            {"label": "If you are a Referral, please mention their name.", "value": "", "type": "text", "required": False},
            {"label": "Are you currently enrolled at, or about to graduate from, a UPenn Engineering Program?", "value": "No", "type": "toggle", "required": True},
            {"label": "Optional unknown preference toggle", "value": "", "type": "toggle", "required": False},
        ]
        res = run_presubmit_review(fields, profile=load_profile())
        self.assertTrue(res["passed"], f"Expected PASS, got failures: {res['failures']}")


class TestPlaywrightPortalSkipOptionalAndConditionals(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_lever_datalabusa_referral_optional_skipped_and_parent_terminated_filled_na(self):
        page = await self.browser.new_page()
        await page.set_content("""
        <form>
          <input type="file" name="resume" />
          <input type="text" name="name" value="Siddhesh Santosh Sawant" />
          <input type="email" name="email" value="siddheshs202001@gmail.com" />
          <div class="application-question">
            <div class="application-label">From which job site did you see this posting?<span class="required">✱</span></div>
            <select required id="source-sel">
              <option value="">Select...</option>
              <option value="LinkedIN">LinkedIN</option>
              <option value="Referral">Referral</option>
              <option value="Other">Other</option>
            </select>
          </div>
          <div class="application-question">
            <div class="application-label">If you are a Referral, please mention their name.</div>
            <input type="text" id="ref-name" />
          </div>
          <div class="application-question">
            <div class="application-label">Have you ever been terminated?<span class="required">✱</span></div>
            <label><input type="radio" name="term" value="Yes" required />Yes</label>
            <label><input type="radio" name="term" value="No" required />No</label>
          </div>
          <div class="application-question">
            <div class="application-label">Please tell company name<span class="required">✱</span></div>
            <input type="text" id="term-co" required />
          </div>
          <div class="application-question">
            <div class="application-label">What is your favorite board game?</div>
            <input type="text" id="opt-unknown" />
          </div>
        </form>
        """)
        filler = UnifiedATSFormFiller(page, load_profile(), "lever")
        await filler._fill_lever(page)
        self.assertEqual(await page.locator("#source-sel").input_value(), "Other")
        self.assertEqual(await page.locator("#ref-name").input_value(), "", "Optional referral field must be left blank")
        self.assertEqual(await page.locator("#term-co").input_value(), "N/A", "Required termination follow-up must be N/A")
        self.assertEqual(await page.locator("#opt-unknown").input_value(), "", "Optional unknown field must be skipped")
        await page.close()

    async def test_ashby_upenn_answered_no_and_optional_unknown_toggle_skipped_without_halt(self):
        page = await self.browser.new_page()
        await page.set_content("""
        <form>
          <div class="ashby-application-form-field-entry" id="card-upenn">
            <label>Are you currently enrolled at, or about to graduate from, a UPenn Engineering Program? *</label>
            <div class="ashby-application-form-input-yesno">
              <button type="button" class="ashby-application-form-input-yesno-option" data-option="yes" onclick="this.setAttribute('aria-pressed','true')">Yes</button>
              <button type="button" class="ashby-application-form-input-yesno-option" data-option="no" onclick="this.setAttribute('aria-pressed','true')">No</button>
            </div>
          </div>
          <div class="ashby-application-form-field-entry" id="card-opt-unknown">
            <label>Do you hold an active FAA commercial pilot license?</label>
            <div class="ashby-application-form-input-yesno">
              <button type="button" class="ashby-application-form-input-yesno-option" data-option="yes" onclick="this.setAttribute('aria-pressed','true')">Yes</button>
              <button type="button" class="ashby-application-form-input-yesno-option" data-option="no" onclick="this.setAttribute('aria-pressed','true')">No</button>
            </div>
          </div>
        </form>
        """)
        filler = UnifiedATSFormFiller(page, load_profile(), "ashby")
        await filler._ashby_toggles(page)
        self.assertEqual(await page.locator("#card-upenn button[data-option='no']").get_attribute("aria-pressed"), "true")
        self.assertIsNone(await page.locator("#card-opt-unknown button[data-option='yes']").get_attribute("aria-pressed"))
        self.assertIsNone(await page.locator("#card-opt-unknown button[data-option='no']").get_attribute("aria-pressed"))
        await page.close()


if __name__ == "__main__":
    unittest.main()
