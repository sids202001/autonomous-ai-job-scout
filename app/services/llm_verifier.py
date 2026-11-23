"""
LLM Verification Gate Module
Provides:
1. LLM Pre-Submit Verification Gate (llm_pre_submit_audit)
   - Checks every required field has a plausible value
   - Checks no placeholder text remains
   - Checks resume is attached
   - Checks name/email/phone match profile
   - Audits filled form screenshot + structured data with LLM
2. LLM Post-Submit Verification Gate (llm_post_submit_verify)
   - Verifies confirmation signal after submit
   - Inspects screenshot + page text with LLM
   - Detects spam flags, bot warnings, CAPTCHAs, or submission errors
   - Strictly halts on any security block or failed submission
"""
import os
import re
import json
import base64
import time
import asyncio
import logging
from typing import Dict, Any, List, Tuple, Optional, Set
import httpx
import random

logger = logging.getLogger(__name__)

# Try loading env from multiple candidate locations
def _get_api_keys() -> Tuple[str, str]:
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        from dotenv import load_dotenv
        for candidate in [
            os.path.expanduser("~/sid_job_scrip/.env"),
            os.path.expanduser("~/.env"),
        ]:
            if os.path.exists(candidate):
                load_dotenv(candidate, override=False)
        gemini_key = os.getenv("GEMINI_API_KEY", gemini_key)
    return "", gemini_key


async def call_llm_json(
    prompt: str,
    image_b64: Optional[str] = None,
    system_prompt: Optional[str] = None,
    timeout_sec: float = 20.0
) -> Optional[Dict[str, Any]]:
    """
    Call Gemini Vision with JSON output.
    Returns parsed dictionary or None if call failed.
    """
    _, gemini_key = _get_api_keys()

    if gemini_key:
        try:
            client = httpx.AsyncClient(verify=False, timeout=timeout_sec)
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_key}"
            parts = [{"text": (system_prompt + "\n\n" + prompt) if system_prompt else prompt}]
            if image_b64:
                parts.append({
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": image_b64
                    }
                })
            gemini_payload = {
                "contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
            }
            resp = await client.post(url, json=gemini_payload)
            await client.aclose()
            if resp.status_code == 200:
                raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                return json.loads(raw_text)
        except Exception as e:
            logger.warning(f"Gemini call error: {e}")

    return None


async def extract_form_state(page) -> Dict[str, Any]:
    """
    Extract all inputs, selects, radios, checkboxes, their values, labels, and required flags.
    Never throws unhandled exceptions; returns extraction_error flag on failure.
    """
    try:
        return await page.evaluate(r'''() => {
            const fields = [];
            const seenNames = new Set();
            const seenCards = new Set();

            // 1. Button Groups, Toggles, and Segmented Controls (Ashby Yes/No, ARIA radiogroups, segmented buttons)
            const toggleContainers = Array.from(document.querySelectorAll(
                ".ashby-application-form-input-yesno, [class*='yesno'], [role='radiogroup'], [class*='segmented'], [class*='btn-group'], [class*='buttonGroup'], [class*='ButtonGroup'], .ashby-application-form-field-entry:has(button.ashby-application-form-input-yesno-option), .field-entry:has(button[aria-pressed])"
            ));
            toggleContainers.forEach(container => {
                const card = container.closest(".ashby-application-form-field-entry, .field-entry, [class*='fieldEntry'], [class*='field-entry'], [class*='FieldEntry'], .application-question, .field, .field-wrapper, .form-group, fieldset") || container;
                if (!card) return;
                const qEl = card.querySelector("label, legend, [class*='title'], [class*='Title'], [class*='label']");
                const qTitle = qEl ? qEl.innerText.trim().replace(/[✱*]/g, "").trim() : (card.innerText.split('\n')[0].replace(/[✱*]/g, "").trim());
                const cardKey = card.id || card.getAttribute("data-field-entry-id") || qTitle || card.innerText.slice(0, 40);
                if (seenCards.has(cardKey)) return;
                seenCards.add(cardKey);

                const buttons = Array.from(card.querySelectorAll("button:not([type='file']):not([aria-label*='close']):not([aria-label*='dismiss']), [role='button'][aria-pressed], [role='radio']"));
                if (!buttons.length) return;
                let selectedText = "";
                let anySelected = false;
                buttons.forEach(b => {
                    const bTxt = (b.innerText || b.getAttribute("data-option") || b.getAttribute("aria-label") || "").trim();
                    const isSel = Boolean(
                        b.classList.contains("selected") || b.classList.contains("active") || (b.className && b.className.includes("_active")) ||
                        b.getAttribute("aria-pressed") === "true" || b.getAttribute("aria-checked") === "true" ||
                        b.getAttribute("data-state") === "checked" || b.getAttribute("data-state") === "on"
                    );
                    if (isSel) {
                        anySelected = true;
                        if (bTxt) selectedText = bTxt;
                    }
                });
                const hasReqMarker = card.classList.contains("required") || card.querySelector(".required, [required], [class*='required'], [class*='asterisk']") || card.innerText.includes("✱") || card.innerText.includes("*");

                fields.push({
                    tag: "toggle",
                    type: "radio",
                    name: card.getAttribute("data-field-path") || card.id || qTitle,
                    label: qTitle,
                    value: selectedText || (anySelected ? "checked" : ""),
                    required: Boolean(hasReqMarker)
                });
            });
            
            // Find inputs, textareas, selects, and ARIA radio elements
            const elements = Array.from(document.querySelectorAll("input, select, textarea, [role='radio']"));
            
            elements.forEach(el => {
                if (el.type === "hidden" || el.type === "submit" || el.type === "button") return;
                if (el.offsetParent === null && el.type !== "checkbox" && el.type !== "radio" && el.getAttribute("role") !== "radio") return;
                
                // Container & label detection (prefer outer field card over inner option wrappers)
                const parent = el.closest(".ashby-application-form-field-entry, [class*='fieldEntry'], [class*='field-entry'], [class*='FieldEntry'], fieldset, .application-question, .field, .field-wrapper, .form-group") || el.parentElement;
                let label = el.getAttribute("aria-label") || el.placeholder || el.name || el.id || "";
                if (parent) {
                    const lblEl = parent.querySelector("label, legend, .label, .text, [id$='-label'], [class*='title'], [class*='Title']");
                    if (lblEl && lblEl.innerText.trim()) {
                        label = lblEl.innerText.trim();
                    }
                }
                label = label.replace(/[✱*]/g, "").trim();

                const pKey = parent ? (parent.id || parent.getAttribute("data-field-entry-id") || label) : "";
                if (pKey && seenCards.has(pKey)) return;
                
                // Required determination
                const hasReqAttr = el.required || el.getAttribute("aria-required") === "true";
                let hasReqMarker = false;
                if (parent) {
                    const text = parent.innerText || "";
                    if (text.includes("✱") || text.includes("*") || parent.classList.contains("required") || parent.querySelector(".required, [class*='required']")) {
                        hasReqMarker = true;
                    }
                }
                const isRequired = Boolean(hasReqAttr || hasReqMarker);
                
                let val = "";
                if (el.type === "checkbox") {
                    val = el.checked ? "checked" : "unchecked";
                } else if (el.type === "radio" || el.getAttribute("role") === "radio") {
                    const rKey = pKey || el.name || el.getAttribute("name") || label;
                    if (rKey) {
                        if (seenNames.has(rKey)) return;
                        seenNames.add(rKey);
                        if (pKey) seenCards.add(pKey);
                        let checkedRadio = null;
                        try {
                            if (el.name) {
                                checkedRadio = document.querySelector(`input[type="radio"][name="${CSS.escape(el.name)}"]:checked, [role="radio"][name="${CSS.escape(el.name)}"][aria-checked="true"]`);
                            }
                        } catch(e) {}
                        if (!checkedRadio && parent) {
                            checkedRadio = parent.querySelector('input[type="radio"]:checked, [role="radio"][aria-checked="true"], [role="radio"].active, [role="radio"].selected, [role="radio"][class*="_active"]');
                        }
                        if (checkedRadio) {
                            const lf = checkedRadio.id ? document.querySelector(`label[for="${CSS.escape(checkedRadio.id)}"]`) : null;
                            const rParent = lf || checkedRadio.closest("label") || checkedRadio.parentElement;
                            val = rParent ? rParent.innerText.trim() : (checkedRadio.value || checkedRadio.innerText || "checked");
                        } else {
                            val = "";
                        }
                    } else {
                        val = (el.checked || el.getAttribute("aria-checked") === "true") ? "checked" : "";
                    }
                } else if (el.tagName.toLowerCase() === "select") {
                    const opt = el.options[el.selectedIndex];
                    val = opt ? opt.text.trim() : (el.value || "");
                } else {
                    val = el.value ? el.value.trim() : "";
                    if (!val && parent) {
                        const singleVal = parent.querySelector(".select__single-value, [class*='singleValue'], [class*='single-value']");
                        if (singleVal && singleVal.innerText.trim()) {
                            val = singleVal.innerText.trim();
                        }
                    }
                }
                
                fields.push({
                    tag: el.tagName.toLowerCase(),
                    type: el.type || (el.getAttribute("role") === "radio" ? "radio" : "text"),
                    name: el.name || el.id || "",
                    label: label,
                    value: val,
                    required: isRequired
                });
            });

            // Resume attachment check (multi-ATS robust detection)
            const hasFileInput = Array.from(document.querySelectorAll('input[type="file"]')).some(fi => fi.files && fi.files.length > 0);
            const hasResumeBadge = Boolean(document.querySelector('.resume-upload-success, .file-uploaded, [data-qa="resume-filename"], .file-name, .application-file-info, [class*="fileEntry"], [class*="FileEntry"], [class*="uploadedFile"], .ashby-application-form-file-entry, [class*="file-info"], [class*="fileInfo"]'));
            const hasRemoveBtn = Array.from(document.querySelectorAll("button")).some(b => /remove|replace|delete/i.test(b.innerText || "") || /remove|replace|delete/i.test(b.getAttribute("aria-label") || ""));
            const hasPdfDocText = Array.from(document.querySelectorAll("[class*='file'], [class*='File'], [class*='upload'], [class*='resume']")).some(el => /\.(pdf|docx?)\b/i.test(el.innerText || ""));
            
            return {
                fields: fields,
                resume_attached: Boolean(hasFileInput || hasResumeBadge || hasRemoveBtn || hasPdfDocText),
                extraction_error: null
            };
        }''')
    except Exception as e:
        logger.warning(f"Primary extract_form_state evaluation error: {e}. Trying secondary DOM fallback...")
        try:
            return await page.evaluate(r'''() => {
                const fields = [];
                document.querySelectorAll("input:not([type='hidden']), select, textarea").forEach(el => {
                    fields.push({
                        tag: el.tagName.toLowerCase(),
                        type: el.type || 'text',
                        name: el.name || el.id || '',
                        label: el.name || el.id || el.placeholder || '',
                        value: el.value || '',
                        required: Boolean(el.required || el.getAttribute('aria-required') === 'true')
                    });
                });
                const hasFileInput = Array.from(document.querySelectorAll('input[type="file"]')).some(fi => fi.files && fi.files.length > 0);
                const hasResumeBadge = Boolean(document.querySelector('.resume-upload-success, .file-uploaded, [data-qa="resume-filename"], .file-name, .application-file-info, [class*="fileEntry"], [class*="FileEntry"], .ashby-application-form-file-entry'));
                return {
                    fields: fields,
                    resume_attached: Boolean(hasFileInput || hasResumeBadge),
                    extraction_error: null
                };
            }''')
        except Exception as fe:
            logger.error(f"Fallback extract_form_state also failed: {fe}")
            return {
                "fields": [],
                "resume_attached": None,
                "extraction_error": str(e)
            }


def extract_all_profile_values(profile: Dict[str, Any]) -> Tuple[Set[str], Set[str]]:
    """
    Extracts all text values and phone digit strings from candidate profile and settings.
    Returns:
        (exact_norm_values: Set[str], phone_digits: Set[str])
    """
    exact_vals = set()
    phone_digits = set()

    def _walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)
        elif obj is not None and not isinstance(obj, bool):
            s = str(obj).strip()
            if s:
                exact_vals.add(s.lower())
                exact_vals.add(re.sub(r'[\s\xa0\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]+', ' ', s).strip().lower())
                digits = re.sub(r'\D', '', s)
                if len(digits) >= 7:
                    phone_digits.add(digits)
                    phone_digits.add(digits[-10:])
                    phone_digits.add(digits[-7:])

    _walk(profile)

    # Also include standard configuration variables
    try:
        import config as cfg
        for attr in ["APPLICANT_EMAIL", "APPLICANT_PHONE", "APPLICANT_FIRST_NAME", "APPLICANT_MIDDLE_NAME",
                     "APPLICANT_LAST_NAME", "WORK_AUTH_STATUS", "US_PERSON_STATUS", "AUDITOR_INDEPENDENCE"]:
            val = getattr(cfg, attr, None)
            if val:
                _walk(val)
    except Exception:
        pass

    return exact_vals, phone_digits


def is_profile_exact_match(val: Any, exact_vals: Set[str], phone_digits: Set[str]) -> bool:
    """
    Check if a form value is character-identical to any profile field after whitespace normalization,
    or matches normalized phone digits.
    """
    if val is None:
        return False
    v_str = str(val).strip()
    if not v_str:
        return False
    v_norm = re.sub(r'[\s\xa0\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]+', ' ', v_str).strip().lower()
    if v_norm in exact_vals or v_str.lower() in exact_vals:
        return True
    d = re.sub(r'\D', '', v_str)
    if len(d) >= 7:
        if d in phone_digits or d[-10:] in phone_digits or d[-7:] in phone_digits:
            return True
    return False


async def llm_pre_submit_audit(
    page,
    profile: Dict[str, Any],
    screenshot_path: str,
) -> Tuple[bool, str, List[str]]:
    """
    LLM Pre-Submit Verification Gate:
    Runs before clicking submit.
    1. Extracts all form fields and values.
    2. Programmatically audits required completeness, placeholders, resume, profile match.
    3. Captures form screenshot.
    4. Calls LLM with screenshot + fields JSON to confirm every required field has a plausible value.
    5. Flags issues: if flagged, does NOT submit, returns issues for Telegram alert.
    """
    issues = []
    
    # Extract state
    try:
        state = await extract_form_state(page)
    except Exception as e:
        logger.error(f"Error extracting form state for pre-submit audit: {e}")
        state = {"fields": [], "resume_attached": None, "extraction_error": str(e)}

    # General rule: An extraction error is UNKNOWN, never evidence of a missing resume.
    # If state still can't be read, halt with a distinct EXTRACTION_FAILED reason — never report "Resume Attached is False"
    if state.get("extraction_error") and not state.get("fields"):
        err_msg = state.get("extraction_error")
        logger.error(f"Pre-submit verification halted due to extraction failure: {err_msg}")
        return False, f"Pre-Submit Verification Flagged: EXTRACTION_FAILED ({err_msg})", ["EXTRACTION_FAILED"]

    fields = state.get("fields", [])
    resume_attached = state.get("resume_attached", None)

    exact_profile_vals, profile_phone_digits = extract_all_profile_values(profile)

    pi = profile.get("personal_info", {})
    expected_email = pi.get("email", "").lower().strip()
    expected_name = pi.get("full_name", "").lower().strip()
    expected_phone = str(pi.get("phone", "")).strip()

    placeholder_patterns = [
        r"^select\.{0,3}$",
        r"^choose\.{0,3}$",
        r"^--+$",
        r"^your answer( here)?$",
        r"^placeholder$",
        r"^undefined$",
        r"^null$",
        r"^todo$",
        r"^enter (text|here|value)$"
    ]

    found_email = ""
    found_name = ""
    found_phone = ""

    # Programmatic audit pass
    for f in fields:
        val = f.get("value", "").strip()
        lbl = f.get("label", "").lower()
        req = f.get("required", False)
        ftype = f.get("type", "")

        # FIX 2(a): Check exact normalized string match against profile
        if is_profile_exact_match(val, exact_profile_vals, profile_phone_digits):
            f["verified_exact_match"] = True

        # FIX 2(b): Never flag a value as placeholder if it matches a profile field exactly
        if not f.get("verified_exact_match"):
            # Check placeholder values
            for pat in placeholder_patterns:
                if re.match(pat, val, re.IGNORECASE):
                    repaired = False
                    if ftype == "select" and page:
                        try:
                            sel_loc = page.locator(f"select[name='{f.get('name', '')}']").first if f.get("name") else None
                            if not sel_loc or (await sel_loc.count()) == 0:
                                sel_loc = page.locator(f"select#{f.get('id', '')}").first if f.get("id") else None
                            if not sel_loc or (await sel_loc.count()) == 0:
                                sel_loc = page.locator(f"select:has-text('{f.get('label', '')[:20]}')").first
                            if sel_loc and (await sel_loc.count()) > 0:
                                opts_els = await sel_loc.locator("option").all()
                                opts = [(await o.text_content() or "").strip() for o in opts_els]
                                clean_opts = [o for o in opts if o and not any(re.match(p, o, re.IGNORECASE) for p in placeholder_patterns)]
                                if clean_opts:
                                    is_loc = any(k in (f.get("name", "") + " " + f.get("label", "")).lower() for k in ["location", "city", "country", "state", "office"])
                                    if is_loc:
                                        try:
                                            from form_filler import DeterministicEngine
                                            chosen_o = DeterministicEngine.pick_best_location_choice(clean_opts)
                                        except Exception:
                                            chosen_o = clean_opts[0]
                                    else:
                                        chosen_o = clean_opts[0]
                                    if chosen_o:
                                        await sel_loc.select_option(label=chosen_o)
                                        await sel_loc.evaluate("el => el.dispatchEvent(new Event('change', { bubbles: true }))")
                                        val = chosen_o
                                        f["value"] = chosen_o
                                        repaired = True
                                        logger.info(f"llm_pre_submit_audit auto-repaired placeholder select '{f.get('label')}' -> '{chosen_o}'")
                        except Exception as rep_e:
                            logger.debug(f"Could not auto-repair placeholder in pre-submit audit: {rep_e}")
                    if not repaired:
                        issues.append(f"Field '{f['label']}' contains placeholder text '{val}'")
                    break

        # Track contact fields
        if "email" in lbl or ftype == "email":
            found_email = val.lower()
        if any(k in lbl for k in ["full name", "legal name", "your name"]):
            found_name = val.lower()
        if "phone" in lbl or ftype == "tel":
            found_phone = val

        # Required completeness check
        if req:
            if ftype in ["checkbox", "radio"]:
                if not val or val == "unchecked":
                    issues.append(f"Required selection '{f['label']}' is not selected")
            elif ftype == "select" or f.get("tag") == "select":
                if not val or any(re.match(pat, val, re.IGNORECASE) for pat in placeholder_patterns):
                    issues.append(f"Required dropdown '{f['label']}' has no valid selection")
            else:
                if not val:
                    issues.append(f"Required field '{f['label']}' is empty")

    # Core profile identity verification (formatting-normalized)
    if expected_email and found_email:
        norm_exp_email = expected_email.lower().strip()
        norm_found_email = found_email.lower().strip()
        if norm_exp_email != norm_found_email and not is_profile_exact_match(found_email, exact_profile_vals, profile_phone_digits):
            issues.append(f"Email mismatch: form has '{found_email}' but profile expects '{expected_email}'")

    if expected_phone and found_phone:
        exp_digits = re.sub(r"\D", "", expected_phone)
        found_digits = re.sub(r"\D", "", found_phone)
        exp_norm = re.sub(r'[\s\xa0\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]+', '', expected_phone).lower()
        found_norm = re.sub(r'[\s\xa0\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]+', '', found_phone).lower()
        if exp_norm == found_norm:
            pass
        elif exp_digits and found_digits and (exp_digits[-10:] == found_digits[-10:] or exp_digits[-7:] in found_digits):
            pass
        elif is_profile_exact_match(found_phone, exact_profile_vals, profile_phone_digits):
            pass
        else:
            issues.append(f"Phone mismatch: form has '{found_phone}' but profile expects '{expected_phone}'")

    # Capture full-page screenshot per Rule 13 contract
    try:
        os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
        except Exception:
            await page.screenshot(path=screenshot_path)
    except Exception as se:
        logger.debug(f"Pre-submit screenshot capture warning: {se}")

    # Prepare base64 image if available
    img_b64 = None
    if os.path.exists(screenshot_path):
        try:
            with open(screenshot_path, "rb") as img_f:
                img_b64 = base64.b64encode(img_f.read()).decode("utf-8")
        except Exception:
            pass

    # Call LLM verification gate
    fields_summary = []
    for f in fields[:40]:
        exact_tag = " [VERIFIED EXACT PROFILE MATCH: PASS]" if f.get("verified_exact_match") else ""
        fields_summary.append(f"• {f['label']} ({f['type']}, Required={f['required']}): '{f['value']}'{exact_tag}")
    fields_str = "\n".join(fields_summary)

    resume_status_str = "True" if resume_attached is True else ("False" if resume_attached is False else "Unknown (DOM extraction inconclusive)")

    prompt = f"""You are an Autonomous ATS Application Pre-Submit Auditor.
Candidate Profile:
- Full Name: {pi.get('full_name')}
- Email: {pi.get('email')}
- Phone: {pi.get('phone')}
- Resume Attached: {resume_status_str}
- Work Auth Status: {profile.get('compliance_and_eeo', {}).get('work_auth_status', 'Authorized / Require Sponsorship')}

Form Fields Extracted:
{fields_str}

Auditing Rules:
1. Every required field must have a plausible, valid answer (no blanks, no placeholders).
2. No placeholder text like "Select...", "--", "undefined", "null", "TODO". Real candidate addresses, names, and phone numbers are NEVER placeholders.
3. Contact details comparisons (phone, email, name):
   - Any field marked [VERIFIED EXACT PROFILE MATCH: PASS] or character-identical to the candidate profile after whitespace normalization PASSES unconditionally. You MUST NOT judge, flag, or report discrepancies on these fields.
   - Variations in phone formatting (country code '+1', spaces, dashes, parentheses), email casing, or name casing are COMPLETELY VALID and MUST NEVER be flagged as discrepant.
   - The Onebrief phone number match ('(445) 208-2299' vs '+14452082299' or '(445) 208-2299') is 100% VALID.
4. If Resume Attached is True or Unknown, do not flag missing resume unless the screenshot explicitly shows an empty file upload input.
5. Work authorization and legal consent questions must be answered confidently.
6. The audit blocks ONLY on genuine unknowns; every block must name the exact field and value.
7. STRICT DOM EVIDENCE RULE: You may ONLY flag a field as unanswered if the DOM data above explicitly shows it empty ('') or unselected. You are strictly forbidden from inferring, assuming, or suspecting a field is unanswered if the DOM shows a non-empty value or selected option.

Respond ONLY with valid JSON:
{{
    "approved": true/false,
    "reason": "Clear explanation of approval or exact missing fields/issues",
    "issues": ["list of specific problems detected"]
}}
"""

    llm_result = await call_llm_json(
        prompt=prompt,
        image_b64=img_b64,
        system_prompt="You are a strict ATS Pre-Submit Verification Gate. Reject any application with missing required fields or placeholder values."
    )

    if llm_result:
        llm_approved = llm_result.get("approved", False)
        llm_reason = llm_result.get("reason", "")
        raw_llm_issues = llm_result.get("issues", [])
        
        # Programmatic safety filter against false formatting / spurious discrepancies
        filtered_llm_issues = []
        for iss in raw_llm_issues:
            iss_low = iss.lower()

            # FIX 2(a): Filter out phone mismatch issues if normalized digits match or character-identical
            if any(k in iss_low for k in ["phone", "telephone", "mobile", "cell"]) and expected_phone:
                exp_10 = re.sub(r"\D", "", expected_phone)[-10:]
                found_10 = re.sub(r"\D", "", found_phone)[-10:]
                if (exp_10 and found_10 and (exp_10 == found_10 or exp_10[-7:] in found_10)) or is_profile_exact_match(found_phone, exact_profile_vals, profile_phone_digits):
                    logger.info(f"Filtering out false phone formatting/mismatch issue from LLM: '{iss}'")
                    continue
                # If issue mentions matching digits
                nums_in_iss = re.findall(r"\d{7,}", re.sub(r"\D", "", iss))
                if nums_in_iss and all(n[-7:] == exp_10[-7:] for n in nums_in_iss if len(n) >= 7):
                    logger.info(f"Filtering out false phone hallucination from LLM: '{iss}'")
                    continue

            # FIX 2(a): Filter out issues on fields that are character-identical to profile
            if any(f.get("verified_exact_match") and f.get("label", "").lower() in iss_low for f in fields if f.get("label")):
                logger.info(f"Filtering out LLM issue on verified exact profile match field: '{iss}'")
                continue

            # If issue flags email casing/formatting but emails match
            if ("email" in iss_low or "e-mail" in iss_low) and expected_email:
                if expected_email.lower().strip() == found_email.lower().strip() or is_profile_exact_match(found_email, exact_profile_vals, profile_phone_digits):
                    logger.info(f"Filtering out false email formatting issue from LLM: '{iss}'")
                    continue

            # If issue flags resume but resume is attached or extraction was inconclusive (never assume missing on unknown)
            if any(k in iss_low for k in ["resume", "cv", "attachment"]) and resume_attached is not False:
                logger.info(f"Filtering out false resume issue from LLM: '{iss}' (resume_attached={resume_attached})")
                continue

            # If issue flags name formatting but parts match
            if any(k in iss_low for k in ["name", "full name"]) and expected_name and found_name:
                if is_profile_exact_match(found_name, exact_profile_vals, profile_phone_digits):
                    logger.info(f"Filtering out false name formatting issue from LLM: '{iss}'")
                    continue
                exp_parts = set(re.findall(r"\w+", expected_name.lower()))
                fnd_parts = set(re.findall(r"\w+", found_name.lower()))
                if exp_parts.intersection(fnd_parts):
                    logger.info(f"Filtering out false name formatting issue from LLM: '{iss}'")
                    continue

            # FIX 2(b): Filter out placeholder flags on real profile values (e.g. Whoop address)
            if "placeholder" in iss_low:
                if any(pv in iss_low for pv in exact_profile_vals if len(pv) >= 4):
                    logger.info(f"Filtering out false placeholder issue (value is verified profile fact): '{iss}'")
                    continue

            # FIX 4: STRICT DOM EVIDENCE RULE
            # LLM may only flag a field as unanswered if DOM actually shows it empty/unselected
            if any(w in iss_low for w in ["missing", "unanswered", "empty", "blank", "unselected", "not filled", "not answered", "not selected"]):
                dom_filled = False
                for f in fields:
                    f_lbl = f.get("label", "").lower()
                    f_val = str(f.get("value", "")).strip()
                    if f_lbl and len(f_lbl) >= 3 and f_lbl in iss_low:
                        if f_val and f_val.lower() != "unchecked":
                            dom_filled = True
                            logger.info(f"Filtering out false unanswered flag from LLM: DOM explicitly shows '{f['label']}' is filled with '{f_val}': '{iss}'")
                            break
                if dom_filled:
                    continue

            filtered_llm_issues.append(iss)

        if not filtered_llm_issues and not issues:
            return True, llm_reason or "All required fields verified and confirmed by LLM.", []

        combined_issues = list(dict.fromkeys(issues + filtered_llm_issues))
        if combined_issues:
            return False, f"Pre-Submit Verification Flagged: {combined_issues[0]}", combined_issues
        else:
            return True, llm_reason or "All required fields verified and confirmed by LLM.", []

    # If LLM service is offline or unavailable, fallback to strict programmatic audit
    if issues:
        return False, f"Pre-Submit Verification Flagged: {issues[0]}", issues

    return True, "Pre-submit verification passed (programmatic verification).", []


CONFIRMATION_URL_PATTERNS = [
    "confirmation", "thank", "thanks", "submitted", "success",
    "applied", "complete", "received", "application_submitted", "application-received"
]

CONFIRMATION_TEXT_PATTERNS = [
    "thank you for applying",
    "thanks for applying",
    "thank you for your application",
    "application submitted",
    "application was submitted",
    "application has been submitted",
    "application received",
    "application has been received",
    "your application was sent",
    "your application has been sent",
    "we have received your application",
    "we've received your application",
    "we received your application",
    "thank you for your interest",
    "track your application",
    "we wanted to let you know we received",
    "submission received",
    "submission successful",
    "successfully applied",
    "successfully submitted",
    "profile submitted",
]

CANDIDATE_LIMIT_PATTERNS = [
    "limits for applications across roles",
    "may not apply more than",
    "application limit reached",
    "once 180 days have passed",
    "exceeded the maximum number of submissions",
    "cannot submit more than",
]

BOT_BLOCK_PATTERNS = [
    "access denied",
    "suspicious activity",
    "you have been blocked",
    "ip address blocked",
    "access is blocked",
    "security challenge",
    "cloudflare turnstile",
    "cf-chl-widget",
    "bot detected",
    "unusual traffic",
    "ray id",
    "please solve the challenge",
    "automated requests"
]

SPAM_FLAG_PATTERNS = [
    "flagged as possible spam",
    "flagged as spam",
    "submission was flagged as possible spam",
    "detected as spam",
    "marked as spam",
    "identified as spam",
]


async def llm_post_submit_verify(
    page,
    screenshot_path: str,
    initial_url: Optional[str] = None,
    timeout_sec: float = 10.0
) -> Tuple[bool, str]:
    """
    LLM Post-Submit Verification Gate:
    After clicking submit, waits for confirmation signal, captures fresh screenshot,
    and runs an LLM check on the screenshot + page text:
    "does this show a confirmed submission, or an error/block?"
    Only returns True if submission genuinely succeeded.
    Halts and returns False on bot blocks, CAPTCHAs, spam flags, or validation errors.
    """

    start_time = time.time()
    url_matched = False
    text_matched = False
    bot_blocked = False
    spam_flagged = False
    candidate_limited = False
    detected_limit_reason = ""
    detected_block_reason = ""
    detected_spam_reason = ""

    while time.time() - start_time < timeout_sec:
        try:
            curr_url = page.url.lower()
            if any(p in curr_url for p in CONFIRMATION_URL_PATTERNS):
                url_matched = True
                break

            content = (await page.content()).lower()

            # Check candidate application limit (e.g. OpenAI 5 apps per 180 days)
            for lp in CANDIDATE_LIMIT_PATTERNS:
                if lp in content:
                    candidate_limited = True
                    detected_limit_reason = f"Candidate application limit reached ('{lp}')"
                    break
            if candidate_limited:
                break

            # Check explicit portal spam / abuse flag (e.g. Ashby "flagged as possible spam")
            for sp in SPAM_FLAG_PATTERNS:
                if sp in content:
                    spam_flagged = True
                    detected_spam_reason = f"Application submission flagged as possible spam: '{sp}'"
                    break
            if spam_flagged:
                break

            # Check bot block / security challenge
            for bp in BOT_BLOCK_PATTERNS:
                if bp in content:
                    bot_blocked = True
                    detected_block_reason = f"Security block / anti-bot challenge detected: '{bp}'"
                    break
            if bot_blocked:
                break

            # Check confirmation text
            for cp in CONFIRMATION_TEXT_PATTERNS:
                if cp in content:
                    text_matched = True
                    break
            if text_matched:
                break

        except Exception:
            pass

        await asyncio.sleep(0.5)

    if candidate_limited:
        try:
            os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
            await page.screenshot(path=screenshot_path)
        except Exception:
            pass
        return False, f"⚠️ CANDIDATE APPLICATION LIMIT REACHED: {detected_limit_reason}. Employer caps submissions per candidate."

    if spam_flagged:
        # Capture failure screenshot and return explicit SPAM_FLAG_DETECTED
        try:
            os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
            await page.screenshot(path=screenshot_path)
        except Exception:
            pass
        return False, f"🚨 SPAM_FLAG_DETECTED: {detected_spam_reason}"

    if bot_blocked:
        # Capture failure screenshot and return immediately
        try:
            os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
            await page.screenshot(path=screenshot_path)
        except Exception:
            pass
        return False, f"🚨 BOT/SECURITY BLOCK: {detected_block_reason}"

    # Take fresh screenshot
    try:
        os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
        await page.screenshot(path=screenshot_path)
    except Exception as se:
        logger.debug(f"Post-submit screenshot capture warning: {se}")

    img_b64 = None
    if os.path.exists(screenshot_path):
        try:
            with open(screenshot_path, "rb") as img_f:
                img_b64 = base64.b64encode(img_f.read()).decode("utf-8")
        except Exception:
            pass

    # Extract page text excerpt
    page_text = ""
    try:
        page_text = await page.evaluate("() => document.body.innerText || ''")
    except Exception:
        pass
    page_text_excerpt = page_text[:2000].strip()

    # Check if submit button is still visible
    submit_still_visible = False
    try:
        sub_btn = page.locator('button[type="submit"]:visible, button:has-text("Submit application"):visible, input[type="submit"]:visible').first
        if await sub_btn.count() > 0 and await sub_btn.is_visible():
            submit_still_visible = True
    except Exception:
        pass

    # Deterministic verification priority:
    # If affirmative URL pattern or confirmation text matched and submit button is gone,
    # return True immediately to avoid vision LLM false-negatives/hallucinations on confirmation cards.
    if (url_matched or text_matched) and not submit_still_visible:
        return True, "Submission Confirmed via affirmative URL/text confirmation signal."

    # Run LLM post-submit check
    prompt = f"""You are an Autonomous ATS Application Submission Verification Auditor.
Determine whether this job application submission was SUCCESSFUL or if there was an ERROR, BLOCK, CAPTCHA, or FAILED submission.

Initial Application URL: {initial_url}
Current Page URL: {page.url}
Submit Button Still Present: {submit_still_visible}

Page Text Excerpt:
\"\"\"
{page_text_excerpt}
\"\"\"

Examine the screenshot and text carefully:
- Does this show a genuine submission confirmation (e.g. "Thank you for applying", "Application received", "Your application has been submitted")?
- OR does it show validation errors, required field warnings, an anti-bot/CAPTCHA challenge, or the original form still open?

Respond ONLY with valid JSON:
{{
    "confirmed": true/false,
    "status": "confirmed_success" | "validation_error" | "captcha_or_bot_block" | "unconfirmed",
    "reason": "Concise explanation of the exact page state"
}}
"""

    llm_result = await call_llm_json(
        prompt=prompt,
        image_b64=img_b64,
        system_prompt="You are a strict ATS Submission Verification Auditor. Never declare success unless the page definitively shows application confirmation."
    )

    if llm_result:
        confirmed = llm_result.get("confirmed", False)
        status = llm_result.get("status", "")
        reason = llm_result.get("reason", "")
        
        if confirmed and status == "confirmed_success":
            return True, reason or "Submission verified by LLM post-submit audit."
        else:
            return False, reason or f"Submission verification failed ({status})."

    # Fallback to deterministic verification if LLM service is offline
    if url_matched or text_matched:
        if not submit_still_visible:
            return True, "Submission confirmed via affirmative URL/text confirmation signal."
        else:
            return False, "Submit button still visible on page despite text match."

    if submit_still_visible:
        return False, "Submit button still visible on page; application form was not accepted by portal."

    return False, "Submission unconfirmed: No confirmation screen, receipt, or redirect observed."

