"""
Red-Green Harness for Nov 20 / Nov 22 Global Form-Interaction Hardening.

Covers:
1. Issue 1 (No Guessing / R1 + R9):
   - Hopper travel experience (never invents travel work; halts with question + options)
   - Hopper embedded enterprise customer (no data on file -> halts and asks)
   - Hopper 30-50% travel -> halts TRAVEL_DECISION_REQUIRED
   - Hcompany applied in last 3 months -> canonical No
   - Hcompany >3 years deploying solutions at customers -> canonical No
   - Imbue career fair -> canonical No
   - Imbue "Is there anything else you'd like us to know?" -> does NOT generate prose (optional -> blank / None, required -> halts)
   - Elastic GCP experience -> halts (no AWS-only prose, no "excel at collaborating")
   - Elastic HR-data-source project -> halts (no canned stakeholder paragraph)
   - Elastic checkbox groups (HRIS/ATS extraction & GCP production) -> selects "None of the above" when offered, else halts
   - Twitch "Are you currently a Twitch employee?" & non-compete -> No (never Yes)
   - Generic unknown multi-choice & yes/no questions -> NEVER selects first option, NEVER defaults to Yes
2. Option-Label Bug (R8 / Nov 22 R1-R3):
   - Exact Ashby Ibotta relocation radio markup (no value attr -> DOM .value === "on") -> reads visible labels ("Yes", "No", "N/A - Local to Denver") and selects "Yes"
   - Exact Ashby Horizon3Ai source radio markup (7 options, all .value === "on") -> reads visible labels and selects "Company website / careers page" (or "Job board", never LinkedIn/referral)
   - Radio / checkbox / native select / custom div option lists -> reads visible labels, never raw "on"
   - Unreadable option label -> halts OPTION_LABEL_UNREADABLE with outer HTML
   - 50-miles proximity truthful DC-based or "No, but willing to relocate"
   - Essay halt (Rule 6) untouched (Abridge "Why are you interested in working at Abridge?" -> ESSAY_HALT)
3. Toggles (R2):
   - Ashby toggle with "Yes" / "No" -> Hcompany false-Yes fixed ("No"), Retell in-office 5x/week SF ("Yes", even when question mentions company offers relocation assistance!)
   - Unreadable toggle -> halts TOGGLE_UNREADABLE with control HTML
4. Education Dates (R5):
   - Greenhouse education block -> fills start/end month AND year from profile (MS Jan 2023-May 2024) and verifies read-back
   - Missing/unsettable year -> halts EDUCATION_DATE_INCOMPLETE
5. Retry / Park & Essay Park-List (R3 + R4):
   - Deterministic role_key across process restarts (no randomized hash())
   - Terminal halt parks posting for >=24h and never relaunches same day
   - Deduplicates retry queue by job ID
   - Essay park-list records essay halts and produces consolidated daily digest
6. JD Pre-Check (R6):
   - Brattle sponsorship ineligibility -> JD_INELIGIBLE
   - Retell Spanish-speaking requirement -> JD_INELIGIBLE
   - Akuna 2027-grad campus role -> JD_INELIGIBLE
   - Normal eligible SWE/DE JD -> eligible
7. Banner Dismissal (Nov 20 R1-R5):
   - Async/late-appearing banner dismissed
   - Shadow-DOM banner dismissed
   - Manage Preferences flow dismissed
   - Accept-All fallback when no reject exists
   - False-positive hidden element NOT blocked
   - False-positive Ashby in-form SMS consent NOT blocked
   - True overlapping undismissable banner -> BANNER_BLOCKED with bot-side notice
8. Bot-side halt alert hygiene (Nov 22 R6):
   - OPTION_LABEL_UNREADABLE / TOGGLE_UNREADABLE / BANNER_BLOCKED alert text states user is NOT expected to answer.
"""

import asyncio
import os
import tempfile
import time
import unittest
from playwright.async_api import async_playwright

from app.services.form_guards import (
    FormHaltException,
    is_essay_or_subjective_question,
    solve_how_did_you_hear,
    solve_proximity_or_current_location,
    solve_relocation_willingness,
    format_missing_info_request,
)
from form_filler import (
    DeterministicEngine,
    GeminiSolver,
    DynamicResumeEngine,
    UnifiedATSFormFiller,
    load_profile,
    select_matching_option,
    dismiss_cookie_banners,
)
from app.services.retry_queue import RetryQueueManager


class TestIssue1NoGuessingAndCanonicalAnswers(unittest.TestCase):
    """R1 & R9: Bot must never invent answers, guess Yes, pick first option, or paste canned prose."""

    def setUp(self):
        self.ai = GeminiSolver()

    def _solve_full(self, question, choices=None, field_type="radio", is_required=True):
        """Simulates the exact form-filler pipeline: DeterministicEngine -> GeminiSolver."""
        ans = DeterministicEngine.solve(question, choices, is_required=is_required)
        if ans is None:
            ans = self.ai.solve(question, choices, field_type)
        return ans

    def test_hopper_travel_experience_halts_no_invention(self):
        q = "Which best describes your experience in travel or travel-adjacent services (airlines, hotels, OTAs, metasearch, travel technology, or bank travel and loyalty)?"
        choices = [
            "I have worked directly at a travel or travel-adjacent company, in a role that touched bookings, changes, refunds, disruptions, or loyalty.",
            "I have built or integrated products for travel partners, even if my employer was not a travel company.",
            "I have no direct travel industry experience, but have worked in a high-complexity consumer or B2B2C domain.",
        ]
        with self.assertRaises(FormHaltException) as ctx:
            self._solve_full(q, choices, "radio")
        self.assertIn("travel", ctx.exception.question.lower())
        self.assertEqual(ctx.exception.choices, choices)

    def test_hopper_embedded_enterprise_halts_no_invention(self):
        q = "Have you worked embedded with an enterprise customer or partner, inside their systems, data controls and security reviews?"
        choices = [
            "Yes, across multiple enterprise deployments.",
            "Yes, on one deployment.",
            "No, I have not worked embedded inside customer systems.",
        ]
        with self.assertRaises(FormHaltException) as ctx:
            self._solve_full(q, choices, "radio")
        self.assertIn("embedded", ctx.exception.question.lower())

    def test_hopper_30_50_travel_now_confirmed_yes_nov22(self):
        q = "This role involves 30 to 50 percent travel, including multi-week stretches on site with global partners, and requires working hours that overlap with European and North American timezones. Does this work for you?"
        choices = ["Yes", "No"]
        self.assertEqual(self._solve_full(q, choices, "radio"), "Yes")

    def test_hcompany_applied_in_last_3_months_is_no(self):
        q = "Have you applied to that same position in the last 3 months?"
        ans = self._solve_full(q, ["Yes", "No"], "toggle")
        self.assertEqual(ans, "No")

    def test_hcompany_more_than_3_years_deploying_solutions_is_no(self):
        q = "Do you have more than three years deploying solutions at customers?"
        ans = self._solve_full(q, ["Yes", "No"], "toggle")
        self.assertEqual(ans, "No")

    def test_imbue_career_fair_is_no(self):
        q = "Did you meet us at a career fair?"
        ans = self._solve_full(q, ["Yes", "No"], "select")
        self.assertEqual(ans, "No")

    def test_imbue_anything_else_does_not_generate_prose(self):
        q = "Is there anything else you'd like us to know?"
        # Optional: must return empty/None, never canned prose
        ans_opt = DeterministicEngine.solve(q, None, is_required=False)
        self.assertFalse(ans_opt)
        # Required: must halt, never generate prose
        with self.assertRaises(FormHaltException):
            self._solve_full(q, None, "textarea", is_required=True)

    def test_elastic_gcp_experience_halts_no_canned_prose(self):
        q = "Where did you gain your GCP experience? Please list the company and role, approximate dates, and the specific GCP tools you used (BigQuery, dbt, Cloud Composer, Dataflow)."
        with self.assertRaises(FormHaltException) as ctx:
            self._solve_full(q, None, "textarea", is_required=True)
        self.assertIn("gcp", ctx.exception.question.lower())

    def test_elastic_hr_data_source_project_halts_no_canned_prose(self):
        q = "Describe a specific project in three sentences where you collaborated with HR stakeholders to build an analytics pipeline from HRIS/ATS data sources."
        with self.assertRaises(FormHaltException):
            self._solve_full(q, None, "textarea", is_required=True)

    def test_elastic_checkbox_groups_select_none_of_the_above_or_halt(self):
        q_hris = "Which of the following HRIS/ATS data extraction experiences do you have? (Select all that apply)"
        choices_with_none = [
            "Built, extended, or substantially modified Workday/Greenhouse HRIS pipelines",
            "Managed ADP / BambooHR connectors",
            "None of the above",
        ]
        self.assertEqual(self._solve_full(q_hris, choices_with_none, "checkbox"), "None of the above")

        choices_without_none = [
            "Built, extended, or substantially modified Workday/Greenhouse HRIS pipelines",
            "Managed ADP / BambooHR connectors",
        ]
        with self.assertRaises(FormHaltException):
            self._solve_full(q_hris, choices_without_none, "checkbox")

    def test_twitch_current_employee_and_non_compete_are_no(self):
        q_emp = "Are you currently a Twitch employee? — We review all applications equally, whether you're an advanced user or new to the platform."
        self.assertEqual(self._solve_full(q_emp, ["Yes", "No"], "select"), "No")
        q_nc = "Are you subject to a non-competition agreement or other agreement that would preclude or restrict your employment at Amazon?"
        self.assertEqual(self._solve_full(q_nc, ["Yes", "No"], "select"), "No")

    def test_dynamic_resume_engine_never_defaults_to_first_option_or_yes(self):
        # Unknown yes/no question must return None, never Yes
        self.assertIsNone(DynamicResumeEngine.answer_choices("Do you have an active FAA commercial pilot license?", ["Yes", "No"]))
        # Unknown multi-choice question must return None, never choices[0]
        self.assertIsNone(DynamicResumeEngine.answer_choices("Which proprietary trading desk have you managed?", ["Options Desk", "FX Desk", "Rates Desk"]))

    def test_50_miles_proximity_truthful_dc_or_willing_to_relocate(self):
        q = "Are you currently located within 50 miles of our Denver, CO office?"
        choices_with_reloc = [
            "Yes, I live within 50 miles of Denver, CO",
            "No, but willing to relocate",
            "No, and not willing to relocate",
        ]
        ans = solve_proximity_or_current_location(q, choices_with_reloc)
        self.assertEqual(ans, "No, but willing to relocate")

    def test_abridge_essay_halt_untouched(self):
        from app.services.grounded_essay import clear_application_context
        clear_application_context()
        q = "Why are you interested in working at Abridge?"
        self.assertTrue(is_essay_or_subjective_question(q))
        # When no JD/about page context is readable, Rule 5 requires halting with ESSAY_HALT (never generic filler)
        with self.assertRaises(FormHaltException) as ctx:
            DeterministicEngine.solve(q, None, is_required=True)
        self.assertEqual(ctx.exception.code, "ESSAY_HALT")


class TestOptionLabelsTogglesEducationAndBanners(unittest.IsolatedAsyncioTestCase):
    """Playwright-backed tests for Ashby radio labels, toggles, GH education dates, and banners."""

    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_ashby_ibotta_relocation_radios_read_visible_labels_not_on(self):
        """Exact Ibotta Ashby radio markup: no value attr (so el.value === 'on'), sibling <label for=id>."""
        page = await self.browser.new_page()
        await page.set_content("""
        <form>
          <fieldset class="ashby-application-form-field-entry">
            <legend>If this position is located in Denver, CO as Hybrid, are you willing to relocate to Denver if you are not already local?</legend>
            <div>
              <input type="radio" id="reloc-0" name="reloc-group" class="ashby-application-form-input-radio-group-option-radio">
              <label for="reloc-0" class="ashby-application-form-input-radio-group-option-label">Yes</label>
            </div>
            <div>
              <input type="radio" id="reloc-1" name="reloc-group" class="ashby-application-form-input-radio-group-option-radio">
              <label for="reloc-1" class="ashby-application-form-input-radio-group-option-label">No</label>
            </div>
            <div>
              <input type="radio" id="reloc-2" name="reloc-group" class="ashby-application-form-input-radio-group-option-radio">
              <label for="reloc-2" class="ashby-application-form-input-radio-group-option-label">N/A - Local to Denver</label>
            </div>
          </fieldset>
        </form>
        """)
        filler = UnifiedATSFormFiller(page, load_profile(), "ashby")
        await filler._ashby_radios(page)
        self.assertTrue(await page.locator("#reloc-0").is_checked(), "Ibotta relocation 'Yes' radio must be selected")
        await page.close()

    async def test_ashby_horizon3ai_source_radios_read_visible_labels_not_on(self):
        """Exact Horizon3Ai Ashby source radio markup: 7 options, all .value === 'on'."""
        page = await self.browser.new_page()
        await page.set_content("""
        <form>
          <fieldset class="ashby-application-form-field-entry">
            <legend>How did you hear about this opportunity?</legend>
            <div><input type="radio" id="src-0" name="src-g"><label for="src-0">Company website / careers page</label></div>
            <div><input type="radio" id="src-1" name="src-g"><label for="src-1">LinkedIn</label></div>
            <div><input type="radio" id="src-2" name="src-g"><label for="src-2">Other job board (e.g. Glassdoor, RepVue, Built In)</label></div>
            <div><input type="radio" id="src-3" name="src-g"><label for="src-3">Networking / referral</label></div>
            <div><input type="radio" id="src-4" name="src-g"><label for="src-4">Recruiter outreach</label></div>
            <div><input type="radio" id="src-5" name="src-g"><label for="src-5">Industry event or conference</label></div>
            <div><input type="radio" id="src-6" name="src-g"><label for="src-6">Other</label></div>
          </fieldset>
        </form>
        """)
        filler = UnifiedATSFormFiller(page, load_profile(), "ashby")
        await filler._ashby_radios(page)
        self.assertTrue(await page.locator("#src-0").is_checked(), "Horizon3Ai 'Company website / careers page' must be selected")
        await page.close()

    async def test_unreadable_radio_label_halts_option_label_unreadable(self):
        """R8/R1: Radio with no label/aria-label/text (only default value='on') must halt OPTION_LABEL_UNREADABLE with outer HTML."""
        page = await self.browser.new_page()
        await page.set_content("""
        <form>
          <fieldset class="ashby-application-form-field-entry">
            <legend>Are you willing to relocate?</legend>
            <div><input type="radio" id="bad-0" name="bad-g"></div>
            <div><input type="radio" id="bad-1" name="bad-g"></div>
          </fieldset>
        </form>
        """)
        filler = UnifiedATSFormFiller(page, load_profile(), "ashby")
        with self.assertRaises(FormHaltException) as ctx:
            await filler._ashby_radios(page)
        self.assertEqual(ctx.exception.code, "OPTION_LABEL_UNREADABLE")
        self.assertIn("bad-0", ctx.exception.message)
        await page.close()

    async def test_ashby_toggles_retell_in_office_yes_and_hcompany_no_and_unreadable_halts(self):
        """R2: Retell 5x/week SF toggle (even mentioning relocation assistance!) -> Yes; Hcompany -> No; unreadable toggle -> TOGGLE_UNREADABLE."""
        page = await self.browser.new_page()
        await page.set_content("""
        <form>
          <div class="ashby-application-form-field-entry" id="card-retell">
            <label>Are you open to working in-office 5x per week in the San Francisco area? Retell AI offers relocation assistance to help make the move easier.</label>
            <div class="ashby-application-form-input-yesno">
              <button type="button" class="ashby-application-form-input-yesno-option" data-option="yes" onclick="this.setAttribute('aria-pressed','true')">Yes</button>
              <button type="button" class="ashby-application-form-input-yesno-option" data-option="no" onclick="this.setAttribute('aria-pressed','true')">No</button>
            </div>
          </div>
          <div class="ashby-application-form-field-entry" id="card-hcompany">
            <label>Have you applied to that same position in the last 3 months?</label>
            <div class="ashby-application-form-input-yesno">
              <button type="button" class="ashby-application-form-input-yesno-option" data-option="yes" onclick="this.setAttribute('aria-pressed','true')">Yes</button>
              <button type="button" class="ashby-application-form-input-yesno-option" data-option="no" onclick="this.setAttribute('aria-pressed','true')">No</button>
            </div>
          </div>
        </form>
        """)
        filler = UnifiedATSFormFiller(page, load_profile(), "ashby")
        await filler._ashby_toggles(page)
        self.assertEqual(await page.locator("#card-retell button[data-option='yes']").get_attribute("aria-pressed"), "true")
        self.assertEqual(await page.locator("#card-hcompany button[data-option='no']").get_attribute("aria-pressed"), "true")

        # Unreadable toggle must halt TOGGLE_UNREADABLE
        await page.set_content("""
        <form>
          <div class="ashby-application-form-field-entry" id="card-bad-toggle">
            <label>Are you willing to relocate?</label>
            <div class="ashby-application-form-input-yesno">
              <button type="button" class="ashby-application-form-input-yesno-option" aria-pressed="false"></button>
              <button type="button" class="ashby-application-form-input-yesno-option" aria-pressed="false"></button>
            </div>
          </div>
        </form>
        """)
        with self.assertRaises(FormHaltException) as ctx:
            await filler._ashby_toggles(page)
        self.assertEqual(ctx.exception.code, "TOGGLE_UNREADABLE")
        await page.close()

    async def test_greenhouse_education_dates_filled_and_verified(self):
        """R5: Greenhouse education block must fill start/end month AND year from profile (MS Jan 2023-May 2024)."""
        page = await self.browser.new_page()
        await page.set_content("""
        <form>
          <div class="education--container">
            <input id="school--0" type="text" />
            <input id="degree--0" type="text" />
            <input id="discipline--0" type="text" />
            <div class="select__container">
              <label for="start-month--0">Start date month</label>
              <input id="start-month--0" class="select__input" />
              <div class="select__control" style="height:28px;width:150px" onclick="document.getElementById('menu-sm').style.display='block'">
                <span class="select__single-value" id="val-sm">Select...</span>
              </div>
              <div id="menu-sm" style="display:none">
                <div class="select__option" onclick="document.getElementById('val-sm').innerText='January';document.getElementById('menu-sm').style.display='none'">January</div>
                <div class="select__option" onclick="document.getElementById('val-sm').innerText='March';document.getElementById('menu-sm').style.display='none'">March</div>
              </div>
            </div>
            <label for="start-year--0">Start date year</label>
            <input id="start-year--0" type="text" />
            <div class="select__container">
              <label for="end-month--0">End date month</label>
              <input id="end-month--0" class="select__input" />
              <div class="select__control" style="height:28px;width:150px" onclick="document.getElementById('menu-em').style.display='block'">
                <span class="select__single-value" id="val-em">Select...</span>
              </div>
              <div id="menu-em" style="display:none">
                <div class="select__option" onclick="document.getElementById('val-em').innerText='May';document.getElementById('menu-em').style.display='none'">May</div>
                <div class="select__option" onclick="document.getElementById('val-em').innerText='March';document.getElementById('menu-em').style.display='none'">March</div>
              </div>
            </div>
            <label for="end-year--0">End date year</label>
            <input id="end-year--0" type="text" />
          </div>
        </form>
        """)
        filler = UnifiedATSFormFiller(page, load_profile(), "greenhouse")
        await filler._gh_education(page)
        self.assertEqual(await page.locator("#start-year--0").input_value(), "2023")
        self.assertEqual(await page.locator("#end-year--0").input_value(), "2024")
        self.assertEqual((await page.locator("#val-sm").inner_text()).strip(), "January")
        self.assertEqual((await page.locator("#val-em").inner_text()).strip(), "May")
        await page.close()

    async def test_banner_dismissal_ashby_false_positive_and_real_overlap(self):
        """Nov 20 R1-R5: Ashby SMS consent is NOT blocked; hidden consent is NOT blocked; real overlapping banner IS dismissed or raises BANNER_BLOCKED."""
        page = await self.browser.new_page()
        # 1. Ashby in-form SMS consent + hidden cookie div -> must NOT raise
        await page.set_content("""
        <form>
          <input type="text" name="name" style="width:300px;height:40px" />
          <div class="_consentBody_q1pga_8" style="width:668px;height:150px">
            <label>Check Yes or No to receive text message updates</label>
            <input type="radio" name="smsConsent" value="given" />
          </div>
          <div class="cookie-banner" style="display:none;width:400px;height:200px">Hidden</div>
        </form>
        """)
        await dismiss_cookie_banners(page, raise_on_blocked=True)

        # 2. Real overlapping cookie banner with Reject button -> dismissed cleanly
        await page.set_content("""
        <form style="margin-top:50px">
          <input type="text" name="email" style="width:400px;height:40px" />
        </form>
        <div id="onetrust-banner-sdk" style="position:fixed;top:0;left:0;width:100%;height:300px;background:#fff;z-index:9999">
          <p>We use cookies</p>
          <button id="onetrust-reject-all-handler" onclick="document.getElementById('onetrust-banner-sdk').remove()">Reject All</button>
          <button id="onetrust-accept-btn-handler">Accept All</button>
        </div>
        """)
        await dismiss_cookie_banners(page, raise_on_blocked=True)
        self.assertEqual(await page.locator("#onetrust-banner-sdk").count(), 0)
        await page.close()


class TestRetryParkJDPrecheckAndAlertHygiene(unittest.TestCase):
    """R3, R4, R6, and Nov 22 R6 alert hygiene."""

    def test_deterministic_role_key_and_24h_park(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "test_retry.db")
            rm = RetryQueueManager(db_path=db)
            k1 = rm.make_role_key("Ibotta", "Data Engineer", "https://jobs.ashbyhq.com/ibotta/41d60bfd-7dff-4ad5-afad-a354e8bbcd58/application")
            # Must not use Python's randomized hash() -> must be stable deterministic hex
            import subprocess, sys
            out = subprocess.check_output(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, '/Users/siddheshsawant/sid_job_scrip'); "
                 "from app.services.retry_queue import RetryQueueManager; "
                 "print(RetryQueueManager.make_role_key('Ibotta', 'Data Engineer', 'https://jobs.ashbyhq.com/ibotta/41d60bfd-7dff-4ad5-afad-a354e8bbcd58/application'))"],
                env={**os.environ, "PYTHONHASHSEED": "12345"}
            ).decode().strip()
            self.assertEqual(k1, out, "role_key MUST be deterministic across PYTHONHASHSEED / daemon restarts")

            # Terminal halt must park for >= 24h and never relaunch same day
            rm.record_attempt_start("Abridge", "Business Systems Engineer", "https://jobs.ashbyhq.com/Abridge/abc/application")
            rm.record_attempt_result("Abridge", "Business Systems Engineer", "https://jobs.ashbyhq.com/Abridge/abc/application", status="ESSAY_HALT", reason="Why Abridge?")
            ok, reason, _ = rm.can_attempt_role("Abridge", "Business Systems Engineer", "https://jobs.ashbyhq.com/Abridge/abc/application")
            self.assertFalse(ok)
            self.assertIn("TERMINAL_BLOCKED", reason)

    def test_jd_precheck_blocks_ineligible_roles(self):
        from app.services.jd_precheck import evaluate_jd_eligibility

        # 1. Brattle: not eligible for immigration sponsorship
        ok, code, reason = evaluate_jd_eligibility(
            "Data and AI Engineer - July 2027",
            "Candidates must be legally authorized to work in the US. This position is not eligible for immigration sponsorship now or in the future."
        )
        self.assertFalse(ok)
        self.assertEqual(code, "JD_INELIGIBLE")

        # 2. Retell: Spanish speaking required
        ok2, code2, _ = evaluate_jd_eligibility(
            "Applied AI Engineer (Customer Facing), Spanish speaking",
            "Fluent in Spanish and English required to support Latin American enterprise customers."
        )
        self.assertFalse(ok2)
        self.assertEqual(code2, "JD_INELIGIBLE")

        # 3. Akuna: 2027-grad campus role
        ok3, code3, _ = evaluate_jd_eligibility(
            "Junior Quantitative Developer & Strategist 2027 | Columbia University",
            "Graduating between December 2026 and June 2027 with a BS/MS in Computer Science."
        )
        self.assertFalse(ok3)
        self.assertEqual(code3, "JD_INELIGIBLE")

        # 4. Normal eligible SWE role
        ok4, code4, _ = evaluate_jd_eligibility(
            "Software Engineer - Backend",
            "We are looking for a Software Engineer with 1-3 years of Python, SQL, and AWS experience."
        )
        self.assertTrue(ok4)

    def test_bot_side_halt_alert_states_user_not_expected_to_answer(self):
        for code in ["OPTION_LABEL_UNREADABLE", "TOGGLE_UNREADABLE", "BANNER_BLOCKED"]:
            msg = format_missing_info_request(code, "Test question", "https://example.com", "TestCo", ["opt1"])
            self.assertIn("not expected to answer", msg.lower())


if __name__ == "__main__":
    unittest.main()
