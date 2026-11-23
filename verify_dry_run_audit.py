import asyncio
import os
import sys
from playwright.async_api import async_playwright

# Ensure project root in sys.path
sys.path.insert(0, "/Users/siddheshsawant/sid_job_scrip")

from form_filler import HybridATSFormFiller, ComboboxHandler, DeterministicEngine
from app.services.presubmit_gate import PresubmitGate, SecurityChallengeDetector
import json

ASHBY_MOCK_HTML = """
<!DOCTYPE html>
<html>
<head><title>Ashby Form Dry Run</title></head>
<body>
  <form id="ashby-form">
    <!-- Personal Info -->
    <div class="ashby-application-form-field-entry">
      <label>Full Name ✱</label>
      <input id="_systemfield_name" name="_systemfield_name" type="text" required />
    </div>
    <div class="ashby-application-form-field-entry">
      <label>Email ✱</label>
      <input id="_systemfield_email" name="_systemfield_email" type="email" required />
    </div>
    <div class="ashby-application-form-field-entry">
      <label>Phone ✱</label>
      <input type="tel" name="phone" required />
    </div>

    <!-- Location Combobox -->
    <div class="ashby-application-form-field-entry">
      <label>Where are you located? (Payroll Tax Location) ✱</label>
      <input class="ashby-application-form-input-autocomplete" role="combobox" aria-label="Location" required />
      <div class="ashby-application-form-input-autocomplete-popup" style="display:none;">
        <div role="option">New York, NY</div>
        <div role="option">Washington, District of Columbia, United States</div>
        <div role="option">San Francisco, CA</div>
      </div>
    </div>

    <!-- Education Level Combobox -->
    <div class="ashby-application-form-field-entry">
      <label>Highest Level of Education ✱</label>
      <button class="toggleButton" type="button">Toggle</button>
      <input class="ashby-application-form-input-autocomplete" role="combobox" aria-label="Highest Level of Education" required />
      <div class="ashby-application-form-input-autocomplete-popup" style="display:none;">
        <div role="option">High School Diploma</div>
        <div role="option">Bachelor's Degree</div>
        <div role="option">Master's Degree</div>
        <div role="option">Doctorate</div>
      </div>
    </div>

    <!-- Clearance Level Follow-up -->
    <div class="ashby-application-form-field-entry">
      <label>What level is your US government security clearance? ✱</label>
      <input class="ashby-application-form-input-autocomplete" role="combobox" aria-label="Clearance Level" required />
      <div class="ashby-application-form-input-autocomplete-popup" style="display:none;">
        <div role="option">N/A</div>
        <div role="option">Confidential</div>
        <div role="option">Secret</div>
        <div role="option">Top Secret</div>
      </div>
    </div>

    <!-- Export Compliance ARIA Radio Group -->
    <div class="ashby-application-form-field-entry" data-field-path="export_compliance">
      <legend>Export Compliance ✱</legend>
      <div role="radiogroup" aria-label="Export Compliance">
        <div role="radio" id="exp_yes" name="export_compliance" aria-checked="false" tabindex="0">I am currently a "U.S. Person"</div>
        <div role="radio" id="exp_no" name="export_compliance" aria-checked="false" tabindex="0">I am not a U.S. Person (Citizen of India, F-1 OPT)</div>
      </div>
    </div>

    <!-- Passive reCAPTCHA v3 Badge (must NOT halt) -->
    <div class="grecaptcha-badge" style="width: 256px; height: 60px; display: block;">
      <iframe src="https://www.google.com/recaptcha/api2/anchor?size=invisible"></iframe>
    </div>
  </form>
  <script>
    // Simulate popup display when autocomplete is clicked or toggle button clicked
    document.querySelectorAll('.ashby-application-form-field-entry').forEach(card => {
      const inp = card.querySelector('input.ashby-application-form-input-autocomplete');
      const btn = card.querySelector('.toggleButton');
      const popup = card.querySelector('.ashby-application-form-input-autocomplete-popup');
      if (inp && popup) {
        inp.addEventListener('click', () => { popup.style.display = 'block'; });
        inp.addEventListener('input', () => { popup.style.display = 'block'; });
      }
      if (btn && popup) {
        btn.addEventListener('click', () => { popup.style.display = 'block'; });
      }
      if (popup) {
        popup.querySelectorAll('[role="option"]').forEach(opt => {
          opt.addEventListener('click', () => {
            if (inp) {
              inp.value = opt.innerText.trim();
              inp.dispatchEvent(new Event('input', {bubbles: true}));
              inp.dispatchEvent(new Event('change', {bubbles: true}));
            }
            popup.style.display = 'none';
          });
        });
      }
    });

    // ARIA Radio click logic
    document.querySelectorAll('[role="radio"]').forEach(r => {
      r.addEventListener('click', () => {
        const group = r.closest('[role="radiogroup"]');
        group.querySelectorAll('[role="radio"]').forEach(other => other.setAttribute('aria-checked', 'false'));
        r.setAttribute('aria-checked', 'true');
        r.dispatchEvent(new Event('change', {bubbles: true}));
      });
    });
  </script>
</body>
</html>
"""

GREENHOUSE_MOCK_HTML = """
<!DOCTYPE html>
<html>
<head><title>Greenhouse Form Dry Run</title></head>
<body>
  <form id="greenhouse-form">
    <div class="field">
      <label for="first_name">First Name ✱</label>
      <input id="first_name" name="first_name" type="text" required />
    </div>
    <div class="field">
      <label for="last_name">Last Name ✱</label>
      <input id="last_name" name="last_name" type="text" required />
    </div>
    <div class="field">
      <label for="email">Email ✱</label>
      <input id="email" name="email" type="email" required />
    </div>

    <!-- Phone Country Code & Phone -->
    <div class="field">
      <label for="job_application_phone_country_code">Phone Country Code ✱</label>
      <select id="job_application_phone_country_code" name="phone_country_code" required>
        <option value="">Please select...</option>
        <option value="+44">United Kingdom +44</option>
        <option value="+1">United States +1</option>
        <option value="+91">India +91</option>
      </select>
    </div>
    <div class="field">
      <label for="phone">Phone ✱</label>
      <input id="phone" name="phone" type="tel" required />
    </div>

    <!-- Candidate Location (React-Select structure) -->
    <div class="field" id="candidate-location-field">
      <label for="candidate-location">Candidate Location ✱</label>
      <div id="candidate-location" class="select__container">
        <div class="select__control">
          <input class="select__input" id="candidate-location" role="combobox" aria-label="Candidate Location" required />
          <div class="select__menu" style="display:none;">
            <div class="select__option">New York, NY, USA</div>
            <div class="select__option">Washington, District of Columbia, United States</div>
            <div class="select__option">San Francisco, CA, USA</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Clearance Parent Question -->
    <div class="field">
      <label>Do you have an active security clearance? ✱</label>
      <div>
        <label><input type="radio" name="has_clearance" value="Yes" /> Yes</label>
        <label><input type="radio" name="has_clearance" value="No" /> No</label>
      </div>
    </div>

    <!-- Clearance Level Follow-up -->
    <div class="field">
      <label for="clearance_level">What level is your US government security clearance? ✱</label>
      <select id="clearance_level" name="clearance_level" required>
        <option value="">Select...</option>
        <option value="N/A">N/A</option>
        <option value="Secret">Secret</option>
        <option value="Top Secret">Top Secret</option>
      </select>
    </div>
  </form>
  <script>
    // Simulate React-Select behavior
    const locInp = document.querySelector('#candidate-location .select__input');
    const locMenu = document.querySelector('#candidate-location .select__menu');
    const locControl = document.querySelector('#candidate-location .select__control');
    if (locInp && locMenu) {
      locInp.addEventListener('focus', () => { locMenu.style.display = 'block'; });
      locInp.addEventListener('input', () => { locMenu.style.display = 'block'; });
      locMenu.querySelectorAll('.select__option').forEach(opt => {
        opt.addEventListener('click', () => {
          locInp.value = ''; // React-Select clears input.value!
          let sv = locControl.querySelector('.select__single-value');
          if (!sv) {
            sv = document.createElement('div');
            sv.className = 'select__single-value';
            locControl.insertBefore(sv, locInp);
          }
          sv.innerText = opt.innerText.trim();
          locMenu.style.display = 'none';
          locInp.dispatchEvent(new Event('change', {bubbles: true}));
        });
      });
    }
  </script>
</body>
</html>
"""

LEVER_MOCK_HTML = """
<!DOCTYPE html>
<html>
<head><title>Lever Form Dry Run</title></head>
<body>
  <form id="application-form">
    <div class="application-question">
      <div class="text">Full Name ✱</div>
      <input name="name" type="text" required />
    </div>
    <div class="application-question">
      <div class="text">Email ✱</div>
      <input name="email" type="email" required />
    </div>
    <div class="application-question">
      <div class="text">Phone ✱</div>
      <input name="phone" type="tel" required />
    </div>
    <div class="application-question">
      <div class="text">Current company</div>
      <input name="org" type="text" />
    </div>
    <div class="application-question">
      <div class="text">LinkedIn URL</div>
      <input name="urls[LinkedIn]" type="text" />
    </div>
    <div class="application-question">
      <div class="text">GitHub URL</div>
      <input name="urls[GitHub]" type="text" />
    </div>
    <div class="application-question">
      <div class="text">Are you legally authorized to work in the United States? ✱</div>
      <label><input type="radio" name="cards[work_auth]" value="Yes" /> Yes</label>
      <label><input type="radio" name="cards[work_auth]" value="No" /> No</label>
    </div>
    <div class="application-question">
      <div class="text">Will you now or in the future require sponsorship for employment visa status? ✱</div>
      <label><input type="radio" name="cards[sponsorship]" value="Yes" /> Yes</label>
      <label><input type="radio" name="cards[sponsorship]" value="No" /> No</label>
    </div>
    <div class="application-question">
      <div class="text">How did you hear about us? ✱</div>
      <label><input type="checkbox" name="cards[referral]" value="LinkedIn" /> LinkedIn</label>
      <label><input type="checkbox" name="cards[referral]" value="Career Fair" /> Career Fair</label>
      <label><input type="checkbox" name="cards[referral]" value="Other" /> Other</label>
    </div>
    <div class="application-question">
      <div class="text">Highest Level of Education ✱</div>
      <select name="cards[education]" required>
        <option value="">Select...</option>
        <option value="Bachelor's Degree">Bachelor's Degree</option>
        <option value="Master's Degree">Master's Degree</option>
        <option value="Doctorate">Doctorate</option>
      </select>
    </div>
    <div class="application-question">
      <div class="text">What is your expected annual compensation? ✱</div>
      <input name="cards[compensation]" type="text" required />
    </div>
    <div class="application-question">
      <div class="text">If you were referred by an employee, please specify their name</div>
      <input name="cards[referral_name]" type="text" />
    </div>
  </form>
</body>
</html>
"""

async def run_dry_run():
    print("=" * 60)
    print("DRY RUN VERIFICATION: Ashby, Greenhouse & Lever Portals")
    print("=" * 60)

    with open("/Users/siddheshsawant/sid_job_scrip/profile.json") as f:
        profile_data = json.load(f)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # -------------------------------------------------------------
        # 1. Ashby Dry Run Pass
        # -------------------------------------------------------------
        print("\n[TEST 1] Ashby Portal Form Dry Run:")
        page = await browser.new_page()
        await page.set_content(ASHBY_MOCK_HTML)

        # Check challenge detector ignores passive badge
        is_chal, chal_name = await SecurityChallengeDetector.detect_challenge(page)
        print(f"  - Passive reCAPTCHA v3 challenge detected? {is_chal} (correctly ignored: {not is_chal})")
        assert not is_chal, "Passive v3 badge should not trigger challenge block"

        filler = HybridATSFormFiller(page, profile_data, portal="ashby")
        await filler._ashby_personal_info(page)
        await filler._ashby_autocompletes(page)
        await filler._ashby_radios(page)

        # Verify Export Compliance selection
        exp_no = page.locator('#exp_no')
        is_exp_no_checked = await exp_no.get_attribute("aria-checked") == "true"
        print(f"  - Export Compliance resolved from locked profile fact: 'Not a U.S. Person' checked={is_exp_no_checked}")
        assert is_exp_no_checked, "Export compliance must select 'Not a U.S. Person' from locked fact"

        # Verify Location
        loc_val = await page.locator('input[aria-label="Location"]').input_value()
        print(f"  - Location resolved to canonical string: '{loc_val}'")
        assert loc_val == "Washington, District of Columbia, United States", f"Unexpected location: {loc_val}"

        # Verify Education Level
        edu_val = await page.locator('input[aria-label="Highest Level of Education"]').input_value()
        print(f"  - Education Level resolved to real DOM option: '{edu_val}'")
        assert edu_val == "Master's Degree", f"Unexpected education: {edu_val}"

        # Verify Clearance Level follow-up
        clr_val = await page.locator('input[aria-label="Clearance Level"]').input_value()
        print(f"  - Clearance follow-up resolved to: '{clr_val}' (never 'No')")
        assert clr_val == "N/A", f"Clearance level follow-up must be 'N/A', got {clr_val}"

        # Pre-submit Gate audit
        gate = PresubmitGate()
        gate_res = await gate.validate_page_state(page, company="Snowflake", portal="ashby")
        print(f"  - Pre-Submit Gate Audit: is_valid={gate_res.is_valid}, blocking_reasons={gate_res.blocking_reasons}")
        assert gate_res.is_valid, f"Ashby pre-submit gate must pass cleanly, got: {gate_res.blocking_reasons}"
        await page.close()

        # -------------------------------------------------------------
        # 2. Greenhouse Dry Run Pass
        # -------------------------------------------------------------
        print("\n[TEST 2] Greenhouse Portal Form Dry Run:")
        page2 = await browser.new_page()
        await page2.set_content(GREENHOUSE_MOCK_HTML)

        filler_gh = HybridATSFormFiller(page2, profile_data, portal="greenhouse")
        await filler_gh._fill_greenhouse(page2)

        # Verify Phone Country Code
        cc_val = await page2.locator('#job_application_phone_country_code').input_value()
        print(f"  - Phone Country Code selected: '{cc_val}' (+1 United States)")
        assert cc_val == "+1", f"Country code must be +1, got {cc_val}"

        # Verify Phone Number
        phone_val = await page2.locator('#phone').input_value()
        print(f"  - Phone number filled: '{phone_val}'")
        assert len(phone_val) > 0, "Phone number must be filled"

        # Verify Candidate Location in React-Select
        sv_el = page2.locator('.select__single-value')
        loc_text = (await sv_el.text_content() or "").strip()
        print(f"  - React-Select Candidate Location resolved to: '{loc_text}'")
        assert loc_text == "Washington, District of Columbia, United States", f"Unexpected candidate location: {loc_text}"

        # Verify Clearance Parent & Follow-up
        c_no = page2.locator('input[name="has_clearance"][value="No"]')
        c_no_checked = await c_no.is_checked()
        print(f"  - Clearance parent question answered: 'No' checked={c_no_checked}")
        assert c_no_checked, "Clearance parent question must be answered 'No'"

        c_level = await page2.locator('#clearance_level').input_value()
        print(f"  - Clearance level follow-up answered: '{c_level}' (never 'No')")
        assert c_level == "N/A", f"Clearance level follow-up must be 'N/A', got {c_level}"

        # Pre-submit Gate audit on Greenhouse
        gate_res_gh = await gate.validate_page_state(page2, company="Together AI", portal="greenhouse")
        print(f"  - Pre-Submit Gate Audit: is_valid={gate_res_gh.is_valid}, blocking_reasons={gate_res_gh.blocking_reasons}")
        assert gate_res_gh.is_valid, f"Greenhouse pre-submit gate must pass cleanly, got: {gate_res_gh.blocking_reasons}"
        await page2.close()

        # -------------------------------------------------------------
        # 3. Lever Dry Run Pass
        # -------------------------------------------------------------
        print("\n[TEST 3] Lever Portal Form Dry Run:")
        page3 = await browser.new_page()
        await page3.set_content(LEVER_MOCK_HTML)

        filler_lever = HybridATSFormFiller(page3, profile_data, portal="lever")
        await filler_lever._fill_lever(page3)

        # Verify Name, Email, Phone
        name_val = await page3.locator('input[name="name"]').input_value()
        email_val = await page3.locator('input[name="email"]').input_value()
        phone_val = await page3.locator('input[name="phone"]').input_value()
        print(f"  - Contact details filled: Name='{name_val}', Email='{email_val}', Phone='{phone_val}'")
        assert len(name_val) > 0 and len(email_val) > 0 and len(phone_val) > 0, "Contact details must be filled"

        # Verify Work Auth & Sponsorship
        wa_yes = page3.locator('input[name="cards[work_auth]"][value="Yes"]')
        wa_checked = await wa_yes.is_checked()
        print(f"  - Work Authorization resolved: 'Yes' checked={wa_checked}")
        assert wa_checked, "Work authorization must be selected 'Yes'"

        sp_yes = page3.locator('input[name="cards[sponsorship]"][value="Yes"]')
        sp_checked = await sp_yes.is_checked()
        print(f"  - Visa Sponsorship resolved: 'Yes' checked={sp_checked}")
        assert sp_checked, "Visa sponsorship must be selected 'Yes'"

        # Verify Education dropdown
        edu_val_lever = await page3.locator('select[name="cards[education]"]').input_value()
        print(f"  - Lever Education dropdown resolved: '{edu_val_lever}'")
        assert edu_val_lever == "Master's Degree", f"Education should be Master's Degree, got {edu_val_lever}"

        # Verify Compensation
        comp_val = await page3.locator('input[name="cards[compensation]"]').input_value()
        print(f"  - Lever Compensation resolved: '{comp_val}'")
        assert len(comp_val) > 0, "Compensation must be filled"

        # Pre-submit Gate audit on Lever
        gate_res_lever = await gate.validate_page_state(page3, company="Datadog", portal="lever")
        print(f"  - Pre-Submit Gate Audit: is_valid={gate_res_lever.is_valid}, blocking_reasons={gate_res_lever.blocking_reasons}")
        assert gate_res_lever.is_valid, f"Lever pre-submit gate must pass cleanly, got: {gate_res_lever.blocking_reasons}"
        await page3.close()

        await browser.close()

    print("\n" + "=" * 60)
    print("ALL DRY RUN PASSES COMPLETED AND 100% GREEN!")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(run_dry_run())
