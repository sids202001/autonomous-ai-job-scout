import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock
from playwright.async_api import async_playwright

from form_filler import (
    normalize_selection_text,
    tokenize_selection_text,
    score_option_match,
    check_numeric_range_match,
    select_matching_option,
)


class TestGenericOptionSelection(unittest.IsolatedAsyncioTestCase):
    """
    Synthetic tests verifying the generic universal option-selection code path
    with never-before-seen question phrasings, custom option structures, and edge cases.
    """

    def test_normalization_and_tokenization(self):
        """Test Unicode whitespace collapsing, punctuation stripping, and tokenization."""
        raw = "  \xa0 Yes, I am legally\u2003authorized! \xa0  "
        norm = normalize_selection_text(raw)
        self.assertEqual(norm, "yes, i am legally authorized")
        tokens = tokenize_selection_text(raw)
        self.assertEqual(tokens, ["yes", "i", "am", "legally", "authorized"])

    def test_numeric_range_scoring(self):
        """Test numeric range bracket matching for salaries and years of experience."""
        # 120,000 in $115,000 - $145,000
        score = score_option_match("120000", "$115,000 - $145,000 / year")
        self.assertGreaterEqual(score, 0.90)

        # 130k in $115k - $145k
        score_k = score_option_match("$130k", "$115k - $145k")
        self.assertGreaterEqual(score_k, 0.90)

        # 4 years in 3 - 5 years of experience
        score_exp = score_option_match("4 years", "3 - 5 years of experience")
        self.assertGreaterEqual(score_exp, 0.90)

        # Out of range should NOT match
        score_out = score_option_match("250000", "$115,000 - $145,000 / year")
        self.assertEqual(score_out, 0.0)

    def test_polarity_and_token_overlap_scoring(self):
        """Test standalone token overlap and polarity equivalence."""
        # 'Yes' matching verbose affirmative
        score_yes = score_option_match("Yes", "Yes, I am authorized to work in the United States")
        self.assertGreaterEqual(score_yes, 0.85)

        # 'Yes' matching 'I agree'
        score_agree = score_option_match("Yes", "I agree to the mandatory data privacy terms")
        self.assertGreaterEqual(score_agree, 0.85)

        # 'No' matching 'Disagree'
        score_no = score_option_match("No", "I disagree with the terms")
        self.assertGreaterEqual(score_no, 0.85)

        # False positive guards: Male vs Female, He vs She, Asian vs Caucasian
        self.assertEqual(score_option_match("male", "Female"), 0.0)
        self.assertEqual(score_option_match("he/him", "she/her"), 0.0)
        self.assertEqual(score_option_match("Asian", "Caucasian"), 0.0)
        self.assertEqual(score_option_match("No", "Yes, I agree"), 0.0)

    async def test_synthetic_live_dom_never_before_seen_phrasings(self):
        """
        Spin up a real headless browser with synthetic HTML containing never-before-seen
        phrasings (deployment cadence tolerance, salary range dropdown, distributed hours agreement).
        Verify that select_matching_option selects each option and DOM state sticks.
        """
        html_content = """<!DOCTYPE html>
        <html>
        <body>
            <!-- Never-before-seen Question 1: Radio group with complex engineering phrasing -->
            <fieldset class="application-question">
                <legend>What is your primary deployment cadence tolerance?</legend>
                <div class="option-row">
                    <label>
                        <input type="radio" name="cadence" value="continuous">
                        Multiple production deploys per day via automated CI/CD trunk-based development
                    </label>
                </div>
                <div class="option-row">
                    <label>
                        <input type="radio" name="cadence" value="sprint">
                        Bi-weekly sprint release candidate staging gate
                    </label>
                </div>
                <div class="option-row">
                    <label>
                        <input type="radio" name="cadence" value="quarterly">
                        Quarterly manual change management board review
                    </label>
                </div>
            </fieldset>

            <!-- Never-before-seen Question 2: Custom dropdown for base compensation bracket -->
            <div class="field-entry">
                <label for="comp_sel">Target Base Compensation Expectations (Annual USD)</label>
                <select id="comp_sel" name="compensation_range">
                    <option value="">-- Please Select --</option>
                    <option value="tier_1">$80,000 - $110,000 / year</option>
                    <option value="tier_2">$115,000 - $145,000 / year</option>
                    <option value="tier_3">$150,000 - $185,000 / year</option>
                </select>
            </div>

            <!-- Never-before-seen Question 3: Button group / toggle for async work agreement -->
            <div class="field-entry">
                <label>Are you willing to adhere to asynchronous distributed core hours?</label>
                <div class="btn-group">
                    <button type="button" data-option="agree">I agree to align with designated team core collaborative hours</button>
                    <button type="button" data-option="disagree">I disagree with mandatory synchronous overlap</button>
                </div>
            </div>

            <!-- Never-before-seen Question 4: Question with no matching candidate options (should halt) -->
            <div class="field-entry">
                <label>What is your active security clearance level?</label>
                <select id="clearance" name="clearance">
                    <option value="">-- Select --</option>
                    <option value="c">Confidential</option>
                    <option value="s">Secret</option>
                    <option value="ts">Top Secret / SCI</option>
                </select>
            </div>
        </body>
        </html>"""

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(html_content)

            # Test 1: Radio match with never-before-seen phrasing
            ok_1 = await select_matching_option(
                page,
                question_text="What is your primary deployment cadence tolerance?",
                target_value="Bi-weekly sprint release candidate"
            )
            self.assertTrue(ok_1)
            # Re-verify DOM state in page directly
            checked_val = await page.evaluate(
                "() => document.querySelector(\"input[name='cadence']:checked\")?.value"
            )
            self.assertEqual(checked_val, "sprint")

            # Test 2: Dropdown match with numeric range
            ok_2 = await select_matching_option(
                page,
                question_text="Target Base Compensation Expectations (Annual USD)",
                target_value="125000"
            )
            self.assertTrue(ok_2)
            sel_text = await page.evaluate(
                "() => document.querySelector('#comp_sel').options[document.querySelector('#comp_sel').selectedIndex].text"
            )
            self.assertIn("$115,000 - $145,000", sel_text)

            # Test 3: Button toggle match with affirmative phrasing
            ok_3 = await select_matching_option(
                page,
                question_text="Are you willing to adhere to asynchronous distributed core hours?",
                target_value="Yes"
            )
            self.assertTrue(ok_3)

            # Test 4: Disqualification / unconfident question must halt without guessing
            ok_4 = await select_matching_option(
                page,
                question_text="What is your active security clearance level?",
                target_value="None / Not Eligible"
            )
            self.assertFalse(ok_4, "Must return False and halt when no option scores above threshold")

            await browser.close()


if __name__ == "__main__":
    unittest.main()
