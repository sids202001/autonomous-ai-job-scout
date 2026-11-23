"""
PRESUBMIT_REVIEW Gate — Deterministic Checklist + Judge-Only LLM Verifier

Hard rule:
- The gate JUDGES ONLY. It NEVER writes into the form, NEVER fixes a value, NEVER invents an answer.
- Submits ONLY if EVERY field is PASS and the deterministic checklist is 100% green.
- Any FAIL or UNCERTAIN -> halts with PRESUBMIT_REVIEW_FAILED naming the exact field, question text,
  filled value, and reason.
"""

import os
import re
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

CANONICAL_DEFAULTS = {
    "github": "https://github.com/sids202001",
    "linkedin": "https://www.linkedin.com/in/sss20",
    "location": "Washington, District of Columbia, United States",
    "home_address": os.getenv("HOME_ADDRESS", ""),
    "salary": "100000",
    "languages": ["english", "marathi", "hindi"],
    "ms_start_month": "january",
    "ms_start_year": "2023",
    "ms_end_month": "may",
    "ms_end_year": "2024",
    "be_start_month": "august",
    "be_start_year": "2018",
    "be_end_month": "august",
    "be_end_year": "2022",
    "auth_expiration": "07/08/2027",
}


def _resolve_home_address(profile: Optional[Dict[str, Any]] = None) -> str:
    """Resolve canonical home address from profile dict, env var, or local profile.json."""
    if isinstance(profile, dict):
        pi = profile.get("personal_info") or {}
        addr = (pi.get("home_address") or pi.get("address") or profile.get("home_address") or "").strip()
        if addr:
            return addr
    env_addr = (os.getenv("HOME_ADDRESS") or CANONICAL_DEFAULTS.get("home_address") or "").strip()
    if env_addr:
        return env_addr
    profile_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "profile.json")
    if os.path.exists(profile_path):
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            pi = data.get("personal_info") or {}
            return (pi.get("home_address") or pi.get("address") or "").strip()
        except Exception:
            pass
    return ""

PLACEHOLDER_VALUES = {
    "", "select", "select...", "choose", "choose...", "please select",
    "select an option", "--", "---", "undefined", "null", "none selected",
}

BANNED_PROSE_FRAGMENTS = [
    "i excel at collaborating with product managers",
    "i am a software & data engineer with 2+ years of experience building",
    "i diagnose complex production issues by analyzing distributed traces",
    "i independently author comprehensive technical documentation",
    "i am excited to bring my engineering background",
]


def _collect_profile_strings(profile: Dict[str, Any]) -> List[str]:
    vals: List[str] = []
    def _walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)
        elif isinstance(obj, str) and obj.strip():
            vals.append(obj.strip().lower())
    _walk(profile or {})
    return vals


def _evaluate_field_deterministic(
    field: Dict[str, Any],
    profile: Optional[Dict[str, Any]] = None,
    profile_strings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Mechanically evaluate one extracted form field against the deterministic checklist.
    Returns: {"field": label, "value": val, "verdict": "PASS"|"FAIL"|"UNCERTAIN", "reason": str}
    """
    label = (field.get("label") or field.get("name") or "").strip()
    q_low = label.lower()
    val = str(field.get("value") if field.get("value") is not None else "").strip()
    val_low = val.lower()
    ftype = (field.get("type") or "text").lower()
    is_req = bool(field.get("required", False))

    def _fail(reason: str) -> Dict[str, Any]:
        return {"field": label, "value": val, "verdict": "FAIL", "reason": reason}

    def _uncertain(reason: str) -> Dict[str, Any]:
        return {"field": label, "value": val, "verdict": "UNCERTAIN", "reason": reason}

    def _pass(reason: str = "Verified against canonical checklist") -> Dict[str, Any]:
        return {"field": label, "value": val, "verdict": "PASS", "reason": reason}

    # 1. Raw HTML default value "on" is NEVER a valid option label
    if val_low == "on":
        return _fail("Option selected by raw HTML default value 'on' instead of visible label")

    # 2. Required field blank / placeholder, or toggle left unselected
    if is_req and val_low in PLACEHOLDER_VALUES:
        return _fail("Required field is blank or still on placeholder value")
    if ftype == "toggle" and val_low in PLACEHOLDER_VALUES:
        return _fail("Yes/No toggle left in default/unselected state")

    # 3. Education months & years completeness (even if not flagged required on DOM)
    if re.search(r"education.*\b(?:start|end)\s*year\b|\b(?:start|end)[-_ ]?year--?\d", q_low):
        if not val:
            return _fail("Education year is blank (must match MS 2023-2024 or BE 2018-2022)")
        if "2" in q_low or "--1" in q_low:
            expected_yr = "2018" if "start" in q_low else "2022"
        else:
            expected_yr = "2023" if "start" in q_low else "2024"
        if val != expected_yr:
            return _fail(f"Education year mismatch: filled '{val}', expected '{expected_yr}'")
        return _pass(f"Education year matches profile ({expected_yr})")

    if re.search(r"education.*\b(?:start|end)\s*month\b|\b(?:start|end)[-_ ]?month--?\d", q_low):
        if not val or val_low in PLACEHOLDER_VALUES:
            return _fail("Education month is blank")
        if "2" in q_low or "--1" in q_low:
            expected_mo = "august"
        else:
            expected_mo = "january" if "start" in q_low else "may"
        if not val_low.startswith(expected_mo[:3]):
            return _fail(f"Education month mismatch: filled '{val}', expected '{expected_mo.title()}'")
        return _pass(f"Education month matches profile ({expected_mo.title()})")

    # If optional and blank (and not education year/month), PASS
    if not val:
        return _pass("Optional field left blank")

    # 4. GitHub / LinkedIn URL (including combined "LinkedIn, Github or any links" fields)
    if "github" in q_low and "linkedin" in q_low:
        if val_low.rstrip("/") not in (
            CANONICAL_DEFAULTS["github"],
            CANONICAL_DEFAULTS["linkedin"],
            "https://siddhesh202001-portfolio.netlify.app",
        ):
            return _fail(f"Profile URL mismatch for combined LinkedIn/GitHub field: '{val}'")
        return _pass("Combined LinkedIn/GitHub URL matches canonical profile")

    if "github" in q_low:
        if val_low.rstrip("/") != CANONICAL_DEFAULTS["github"]:
            return _fail(f"GitHub URL mismatch: '{val}' != '{CANONICAL_DEFAULTS['github']}'")
        return _pass("GitHub URL exact match")

    # 5. LinkedIn URL
    if "linkedin" in q_low:
        if val_low.rstrip("/") != CANONICAL_DEFAULTS["linkedin"]:
            return _fail(f"LinkedIn URL mismatch: '{val}' != '{CANONICAL_DEFAULTS['linkedin']}'")
        return _pass("LinkedIn URL exact match")

    # 6. Home street address
    if re.search(r"\b(?:home|street|residential|physical)\s*address\b|\baddress\s*line\s*1\b", q_low):
        expected_addr = _resolve_home_address(profile)
        if not expected_addr:
            return _fail("Home address is not configured in profile.json")
        norm_val = re.sub(r"[^a-z0-9]", "", val_low)
        norm_exp = re.sub(r"[^a-z0-9]", "", expected_addr.lower())
        if norm_val != norm_exp:
            return _fail(f"Home address mismatch: '{val}' != '{expected_addr}'")
        return _pass("Home address exact match")

    # 7. Location (non-office/non-relocation)
    if re.search(r"^(?:current\s*)?location$|^location\s*\(city\)$", q_low):
        if "washington" not in val_low or ("district of columbia" not in val_low and "dc" not in val_low):
            return _fail(f"Location mismatch: '{val}' does not match '{CANONICAL_DEFAULTS['location']}'")
        return _pass("Location matches Washington, DC")

    # 8. Work authorization
    if re.search(r"authorized\s*to\s*work|legally\s*authorized|eligible\s*to\s*work", q_low) and "expiration" not in q_low:
        if not re.match(r"^yes\b|authorized|eligible", val_low):
            return _fail(f"Work authorization must be Yes, got '{val}'")
        return _pass("Work authorization == Yes")

    # 9. Sponsorship / Visa Support (any phrasing -> canonical Yes)
    if (
        re.search(
            r"require.*sponsorship|need.*sponsorship|visa\s*sponsorship|visa\s*support|"
            r"visa\s*assistance|immigration\s*(?:support|assistance|sponsorship)|"
            r"sponsor.*(?:immigration|visa)|^\s*sponsorship\s*\??\s*$",
            q_low,
        )
        and not re.search(r"\b(which|detail|explain|describe|type|status|expir)\b", q_low)
    ):
        if not re.match(r"^yes\b", val_low):
            return _fail(f"Sponsorship/Visa Support must be Yes, got '{val}'")
        return _pass("Sponsorship / Visa Support == Yes")

    # 10. Which Visa
    if re.search(r"which\s*visa|type\s*of\s*visa|visa\s*type|visa\s*category", q_low):
        if not re.search(r"\bh-?1b\b", val_low):
            return _fail(f"Which Visa must be H-1B, got '{val}'")
        return _pass("Which Visa == H-1B")

    # 11. How did you hear / Source (NEVER LinkedIn, referral, social media)
    if re.search(r"how\s*did\s*you\s*(?:hear|learn|find)|where\s*did\s*you\s*(?:hear|find)|referral\s*source|application\s*source", q_low):
        if any(b in val_low for b in ["linkedin", "referral", "social media", "employee", "recruiter"]):
            return _fail(f"Banned application source selected: '{val}'")
        if not any(ok in val_low for ok in ["company", "website", "career", "job board", "job site", "direct", "other"]):
            return _fail(f"Source '{val}' is not 'Company Website' or 'Job board'")
        return _pass("Source approved (Company Website / Job board)")

    # 12. 50-miles / proximity (truthful DC-based or "No, but willing to relocate")
    if re.search(r"within\s*\d+\s*miles|commut(?:ing|able)\s*distance", q_low):
        is_dc_q = any(k in q_low for k in ["washington", "dc", "district of columbia", "arlington", "alexandria", "virginia"])
        if not is_dc_q and re.match(r"^yes\b", val_low):
            return _fail(f"Untruthful 50-mile proximity answer '{val}' for non-DC office")
        return _pass("Proximity answer truthful for Washington, DC")

    # 13. Relocation willingness
    if re.search(r"willing\s*to\s*relocate|open\s*to\s*relocat", q_low) and "within" not in q_low:
        if not re.match(r"^yes\b", val_low) and "willing to relocate" not in val_low:
            return _fail(f"Relocation willingness must be Yes, got '{val}'")
        return _pass("Relocation == Yes")

    # 14. Salary
    if re.search(r"(?:expected|desired|target|annual)\s*(?:salary|compensation)", q_low):
        digits = re.sub(r"[^0-9]", "", val)
        if digits and digits not in ("100000", "100"):
            return _fail(f"Salary mismatch: filled '{val}', expected 100000")
        return _pass("Salary == 100000")

    # 15. Start date == submit date + 1 month (within +-4 days, supporting YYYY-MM-DD and MM/DD/YYYY)
    if re.search(
        r"(?:available|earliest)\s*start\s*date|earliest\s*you\s*would\s*(?:want|like|be\s*able)\s*to\s*start|"
        r"when\s*can\s*you\s*start|start\s*working\s*with\s*us",
        q_low,
    ):
        filled_dt = None
        m_iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", val)
        m_us = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", val)
        if m_iso:
            try:
                filled_dt = datetime(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
            except Exception:
                return _uncertain(f"Could not parse start date '{val}'")
        elif m_us:
            try:
                filled_dt = datetime(int(m_us.group(3)), int(m_us.group(1)), int(m_us.group(2)))
            except Exception:
                return _uncertain(f"Could not parse start date '{val}'")
        if filled_dt:
            target_dt = datetime.now() + timedelta(days=30)
            if abs((filled_dt - target_dt).days) > 4:
                return _fail(f"Start date '{val}' deviates from submit date + 1 month ({target_dt.strftime('%Y-%m-%d')})")
        return _pass("Start date matches submit date + 1 month")

    # 16. EEO Cross-Field Integrity (catches Reddit veteran="Male", transgender="Yes", sexual_orientation="Asexual", race="American Indian")
    if re.search(r"\btransgender\b", q_low):
        if not re.match(r"^no\b|cisgender|not\s*transgender|decline|prefer\s*not", val_low):
            return _fail(f"Transgender field must be No, got '{val}'")
        return _pass("Transgender == No")

    if re.search(r"sexual\s*orientation", q_low):
        if "asexual" in val_low or not any(ok in val_low for ok in ["heterosexual", "straight", "decline", "prefer not"]):
            return _fail(f"Sexual orientation must be Heterosexual (never Asexual), got '{val}'")
        return _pass("Sexual orientation == Heterosexual")

    if re.search(r"\bveteran\b|military\s*service", q_low):
        if val_low in ("male", "female", "man", "woman", "yes") or (
            "not" not in val_low and "no" not in val_low and "never" not in val_low and "decline" not in val_low
        ):
            return _fail(f"Veteran status invalid / cross-field leak: got '{val}'")
        return _pass("Veteran status == Not a veteran")

    if re.search(r"\bdisabilit", q_low):
        if val_low.startswith("yes") or ("no" not in val_low and "do not" not in val_low and "decline" not in val_low):
            return _fail(f"Disability status invalid: got '{val}'")
        return _pass("Disability == No")

    if re.search(r"\b(?:race|ethnicity|ethnicities|racial)\b", q_low) and "hispanic" not in q_low:
        if "american indian" in val_low or "alaska native" in val_low or "black" in val_low or "white" in val_low or "caucasian" in val_low:
            return _fail(f"Race/Ethnicity mismatch: got '{val}', expected Asian / South Asian")
        if "asian" not in val_low and "decline" not in val_low and "prefer not" not in val_low:
            return _fail(f"Race/Ethnicity mismatch: got '{val}', expected Asian / South Asian")
        return _pass("Race == Asian")

    if re.search(r"\bgender\b", q_low) and "transgender" not in q_low and "pronoun" not in q_low:
        if "female" in val_low or "woman" in val_low or not any(ok in val_low for ok in ["male", "man", "decline", "prefer not"]):
            return _fail(f"Gender mismatch: got '{val}', expected Male")
        return _pass("Gender == Male")

    # 17. Languages
    if re.search(r"\blanguages?\s*(?:spoken|known|fluency)\b", q_low):
        if "spanish" in val_low:
            return _fail(f"Languages contains Spanish (not on record): '{val}'")
        if not all(lang in val_low for lang in ["english", "hindi", "marathi"]):
            return _fail(f"Languages mismatch: got '{val}', expected English, Marathi, Hindi")
        return _pass("Languages == English, Marathi, Hindi")

    # 18. Known false-invention traps (Hopper travel, Hcompany 3 months / >3y, Twitch employee, career fair, non-compete)
    if re.search(r"experience\s+in\s+travel|travel-?adjacent", q_low):
        if any(bad in val_low for bad in ["worked directly at a travel", "built or integrated products for travel"]):
            return _fail(f"Invented travel-industry experience: '{val}'")

    if re.search(r"applied\s+to\s+(?:that|this|the)\s+same\s+(?:position|role|job)|applied.*in\s+the\s+last\s+\d+\s+months", q_low):
        if val_low.startswith("yes"):
            return _fail(f"False prior-application answer '{val}' (expected No)")

    if re.search(r"more\s+than\s+(?:three|3)\s+years?\s+deploying", q_low):
        if val_low.startswith("yes"):
            return _fail(f"False >3 years deploying answer '{val}' (expected No)")

    if re.search(r"are\s+you\s+currently\s+(?:a|an)\s+[\w\s]{1,30}\s+employee|non-?compet", q_low):
        if val_low.startswith("yes"):
            return _fail(f"False current-employee / non-compete answer '{val}' (expected No)")

    if re.search(r"career\s+fair", q_low):
        if val_low.startswith("yes"):
            return _fail(f"False career-fair answer '{val}' (expected No)")

    # 19. Open-Text & Free-Text Grounding Gate:
    #     Generated prose is ALLOWED only for open-text questions where EVERY claim, achievement, and metric is
    #     grounded in profile.json/resume/GitHub/portfolio or the company's JD/about page (and not a Hard Limit in 4).
    if ftype in ("textarea", "text"):
        if any(frag in val_low for frag in BANNED_PROSE_FRAGMENTS):
            return _fail(f"Generated/canned prose detected in free-text field: '{val[:80]}...'")
        try:
            from app.services.grounded_essay import is_open_text_question, verify_open_text_field
            if is_open_text_question(label) or len(val) > 80:
                if profile_strings is not None and val_low in profile_strings:
                    return _pass("Value grounded directly in profile.json")
                ok, why = verify_open_text_field(label, val)
                return _pass(why) if ok else _fail(why)
        except ImportError:
            if len(val) > 80 and profile_strings is not None and val_low not in profile_strings:
                return _fail(f"Unverified free-text prose not in profile.json: '{val[:80]}...'")

    # 20. Record-Grounding Fallback (catches new unknown screening questions even without GEMINI_API_KEY):
    # Verify that the filled value matches either a known profile.json value or DeterministicEngine's canonical answer.
    if profile_strings and any(val_low == ps or (len(val_low) >= 4 and val_low in ps) for ps in profile_strings):
        return _pass("Value grounded directly in profile.json")

    try:
        from form_filler import DeterministicEngine
        from app.services.form_guards import FormHaltException
        expected = DeterministicEngine.solve(label, [val] if ftype in ("radio", "select", "toggle", "checkbox") else None, is_required=is_req)
        if expected is not None and str(expected).strip().lower() == val_low:
            return _pass(f"Matches canonical DeterministicEngine rule ('{expected}')")
        if expected is not None and str(expected).strip().lower() != val_low:
            return _fail(f"Contradicts canonical rule for '{label}': filled '{val}', canonical is '{expected}'")
    except FormHaltException as fhe:
        return _fail(f"Field triggers canonical halt [{fhe.code}]: {fhe.message}")
    except Exception:
        pass

    if is_req:
        return _uncertain(f"No canonical rule or profile.json grounding found for required field '{label}' with value '{val}'")

    return _pass()


def run_presubmit_review(
    fields: List[Dict[str, Any]],
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Synchronous deterministic PRESUBMIT_REVIEW gate.
    Returns structured review dict:
      {
        "passed": bool,
        "decision": "PASS" | "FAIL",
        "halt_code": None | "PRESUBMIT_REVIEW_FAILED",
        "verdicts": [ {field, value, verdict, reason}, ... ],
        "failures": [ ... ],
        "summary": str,
      }
    """
    profile_strings = _collect_profile_strings(profile or {})
    verdicts = [_evaluate_field_deterministic(f, profile, profile_strings) for f in (fields or [])]
    failures = [v for v in verdicts if v["verdict"] in ("FAIL", "UNCERTAIN")]
    passed = len(failures) == 0

    if passed:
        summary = f"PRESUBMIT_REVIEW PASS ({len(verdicts)} fields verified, 0 failures)"
    else:
        first = failures[0]
        summary = (
            f"PRESUBMIT_REVIEW_FAILED ({len(failures)}/{len(verdicts)} failed): "
            f"field='{first['field']}', value='{first['value']}', verdict={first['verdict']}, reason={first['reason']}"
        )

    return {
        "passed": passed,
        "decision": "PASS" if passed else "FAIL",
        "halt_code": None if passed else "PRESUBMIT_REVIEW_FAILED",
        "verdicts": verdicts,
        "failures": failures,
        "summary": summary,
    }


async def llm_judge_fields_against_record(
    fields: List[Dict[str, Any]],
    profile: Optional[Dict[str, Any]] = None,
    timeout_sec: float = 18.0,
) -> Optional[List[Dict[str, Any]]]:
    """
    Judge-Only LLM review step:
    Compares every filled form field against profile.json + verified resume facts.
    Returns a list of per-field verdicts:
      [{"field": str, "value": str, "verdict": "PASS"|"FAIL"|"UNCERTAIN", "reason": str}, ...]
    The LLM JUDGES ONLY — it never writes into the form, never fixes a value, never invents an answer.
    """
    if not fields:
        return []

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    nvidia_key = os.getenv("NVIDIA_API_KEY")
    for p in ["/Users/siddheshsawant/sid_job_scrip/.env", os.path.expanduser("~/.env"), os.path.expanduser("~/.hermes/.env")]:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        if (line.startswith("GEMINI_API_KEY=") or line.startswith("GOOGLE_API_KEY=")) and not api_key:
                            api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("NVIDIA_API_KEY=") and not nvidia_key:
                            nvidia_key = line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass

    if not api_key and not nvidia_key:
        return None

    try:
        import httpx
        from ai_answer_engine import RESUME_TEXT
    except Exception:
        RESUME_TEXT = ""

    compact_fields = [
        {
            "index": i,
            "field": (f.get("label") or f.get("name") or "").strip(),
            "value": str(f.get("value") if f.get("value") is not None else "").strip(),
            "type": f.get("type", "text"),
            "required": bool(f.get("required", False)),
        }
        for i, f in enumerate(fields)
    ]

    system_prompt = (
        "You are a strict JUDGE-ONLY pre-submit compliance auditor for job applications.\n"
        "You NEVER write answers, NEVER fix values, and NEVER guess.\n"
        "Compare each filled field against the candidate's canonical profile.json and resume record below.\n"
        "For EVERY field in the input list, return an object with:\n"
        '  {"index": <int>, "field": <str>, "value": <str>, "verdict": "PASS" | "FAIL" | "UNCERTAIN", "reason": <concise reason>}\n'
        "Rules for verdicts:\n"
        "- PASS: Value is truthful and consistent with profile.json / resume (or optional field left blank).\n"
        "- FAIL: Value contradicts profile.json / resume, claims experience not in the record (e.g. travel industry, GCP, >3 yrs customer deployment), "
        "uses raw HTML 'on', has EEO cross-field leakage, or contains generated essay prose not in profile.json.\n"
        "- EXCEPTION — Open-text / descriptive fields (Why company, role fit, experience in skill/tool, proudest/challenging "
        "project, bug fixed): generated prose is allowed ONLY if EVERY claim, achievement, and metric is supported by the "
        "VERIFIED CANDIDATE FACTS / profile.json / resume (and COMPANY ABOUT PAGE / JOB DESCRIPTION when the question is "
        "about the company/role). Achievement and metric claims ('reduced X from hours to minutes', percentages, 'built Y "
        "that did Z') must match the same achievement/metric in the candidate record — a tool or skill name alone never "
        "grounds an achievement claim. FAIL on generic praise, unowned skills, false achievements/metrics, or prose in "
        "hard-limit fields ('Is there anything else?', self-ratings, age/DOB, weekend/night-shift decisions).\n"
        "- UNCERTAIN: Required field has an unverified claim that cannot be confirmed from profile.json or resume.\n"
        'Output valid JSON: {"verdicts": [ ... ]}'
    )

    try:
        from app.services.grounded_essay import judge_context_for_fields
        essay_ctx = judge_context_for_fields(fields)
    except Exception:
        essay_ctx = ""
    user_prompt = (
        f"CANONICAL PROFILE.JSON:\n{json.dumps(profile or {}, indent=2)}\n\n"
        f"VERIFIED RESUME:\n{RESUME_TEXT}\n\n"
        + (f"WHY-COMPANY ESSAY GROUNDING SOURCES:\n{essay_ctx}\n\n" if essay_ctx else "")
        + f"FORM FIELDS TO JUDGE:\n{json.dumps(compact_fields, indent=2)}"
    )

    if api_key:
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key)
            for model_name in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]:
                try:
                    resp = client.models.generate_content(
                        model=model_name,
                        contents=user_prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            temperature=0.0,
                            response_mime_type="application/json",
                        ),
                    )
                    raw_text = (resp.text or "").strip()
                    if raw_text:
                        parsed = json.loads(raw_text)
                        verdicts = parsed.get("verdicts") if isinstance(parsed, dict) else parsed
                        if isinstance(verdicts, list):
                            return verdicts
                except Exception as me:
                    logger.debug(f"llm_judge Gemini {model_name} error: {me}")
                    continue
        except Exception as e:
            logger.warning(f"llm_judge_fields_against_record Gemini error: {e}")

    if nvidia_key:
        try:
            import httpx
            async with httpx.AsyncClient(verify=False, timeout=timeout_sec) as client:
                for nim_model in [
                    "nvidia/nemotron-3.5-bolt-e4b",
                    "nvidia/nemotron-3.5-lightning-30b-a3b",
                ]:
                    try:
                        resp = await client.post(
                            "https://integrate.api.nvidia.com/v1/chat/completions",
                            headers={"Authorization": f"Bearer {nvidia_key}"},
                            json={
                                "model": nim_model,
                                "messages": [
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt},
                                ],
                                "temperature": 0.0,
                                "max_tokens": 1600,
                            },
                        )
                        if resp.status_code == 200:
                            msg = resp.json()["choices"][0]["message"]
                            raw = (msg.get("content") or "").strip()
                            if not raw:
                                continue
                            # Strip optional markdown code fences
                            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
                            m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", raw)
                            if m:
                                parsed = json.loads(m.group(1))
                                verdicts = parsed.get("verdicts") if isinstance(parsed, dict) else parsed
                                if isinstance(verdicts, list):
                                    return verdicts
                    except Exception as ne:
                        logger.debug(f"llm_judge NIM {nim_model} error: {ne}")
                        continue
        except Exception as e:
            logger.warning(f"llm_judge_fields_against_record NIM error: {e}")

    return None


async def extract_dom_fields_for_review(page) -> List[Dict[str, Any]]:
    """Extract every visible form field (label-first, never raw 'on') for PRESUBMIT_REVIEW."""
    return await page.evaluate(r'''() => {
        const fields = [];
        const seenRadioGroups = new Set();

        // 1. Toggle button groups (deduped by field card, must contain >=2 toggle buttons)
        const seenToggleCards = new Set();
        document.querySelectorAll('.ashby-application-form-field-entry, [class*="fieldEntry"], fieldset').forEach(card => {
            const btns = Array.from(card.querySelectorAll('button[data-option], button.ashby-application-form-input-yesno-option, [class*="yesno"] button, button[aria-pressed]'));
            if (btns.length < 2) return;
            const qEl = card.querySelector('label, legend, [class*="title"]');
            const q = qEl ? qEl.innerText.replace(/[✱*]/g, '').trim() : '';
            if (!q || seenToggleCards.has(q)) return;
            seenToggleCards.add(q);
            let sel = '';
            btns.forEach(b => {
                if (b.getAttribute('aria-pressed') === 'true' || b.getAttribute('aria-checked') === 'true' || /active|selected|_active_/.test(b.className)) {
                    sel = (b.innerText || b.getAttribute('data-option') || '').trim();
                }
            });
            const req = Boolean(card.querySelector('[required], .required, [class*="required"]') || card.innerText.includes('*') || card.innerText.includes('✱'));
            fields.push({label: q, value: sel, type: 'toggle', required: req});
        });

        // 2. Radio groups (visible label first!)
        document.querySelectorAll('input[type="radio"]').forEach(r => {
            const key = r.name || r.id;
            if (seenRadioGroups.has(key)) return;
            seenRadioGroups.add(key);
            const grp = r.name ? Array.from(document.querySelectorAll(`input[type="radio"][name="${CSS.escape(r.name)}"]`)) : [r];
            const card = r.closest('fieldset, .ashby-application-form-field-entry, [class*="fieldEntry"], .application-question, .field');
            const qEl = card ? card.querySelector('legend, label, [class*="title"]') : null;
            const q = qEl ? qEl.innerText.replace(/[✱*]/g, '').trim() : (r.name || '');
            let sel = '';
            grp.forEach(opt => {
                if (opt.checked) {
                    const lf = opt.id ? document.querySelector(`label[for="${CSS.escape(opt.id)}"]`) : null;
                    const cl = opt.closest('label');
                    sel = (lf ? lf.innerText : (cl ? cl.innerText : (opt.getAttribute('aria-label') || opt.getAttribute('value') || ''))).trim();
                    if (!sel && opt.value) sel = opt.value;
                }
            });
            const req = Boolean(grp.some(x => x.required) || (card && (card.innerText.includes('*') || card.innerText.includes('✱'))));
            if (q) fields.push({label: q, value: sel, type: 'radio', required: req});
        });

        // 3. Text, email, url, tel, date, textarea, native select
        document.querySelectorAll('input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]):not([type="file"]):not([type="submit"]), textarea, select').forEach(el => {
            if (el.offsetParent === null) return;
            if (el.classList.contains('select__input')) return;
            const lf = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
            const card = el.closest('.ashby-application-form-field-entry, [class*="fieldEntry"], .field, .application-question, fieldset') || el.parentElement;
            const qEl = lf || (card ? card.querySelector('label, legend, [class*="title"]') : null);
            const q = (qEl ? qEl.innerText : (el.getAttribute('aria-label') || el.placeholder || el.name || el.id || '')).replace(/[✱*]/g, '').trim();
            let val = '';
            if (el.tagName.toLowerCase() === 'select') {
                const opt = el.options[el.selectedIndex];
                val = opt ? opt.text.trim() : '';
            } else {
                val = (el.value || '').trim();
            }
            const req = Boolean(el.required || el.getAttribute('aria-required') === 'true' || (card && (card.innerText.includes('*') || card.innerText.includes('✱'))));
            if (q) fields.push({label: q, value: val, type: el.tagName.toLowerCase() === 'textarea' ? 'textarea' : (el.type || 'text'), required: req});
        });

        // 4. React-Select containers (Greenhouse)
        document.querySelectorAll('.select__container').forEach(cont => {
            if (cont.offsetParent === null) return;
            const lbl = cont.querySelector('label');
            const valEl = cont.querySelector('.select__single-value, .select__multi-value__label');
            const q = lbl ? lbl.innerText.replace(/[✱*]/g, '').trim() : '';
            const val = valEl ? valEl.innerText.trim() : '';
            const req = Boolean(cont.innerText.includes('*') || cont.querySelector('[aria-required="true"]'));
            if (q) fields.push({label: q, value: val, type: 'select', required: req});
        });

        return fields;
    }''')


async def run_presubmit_review_async(
    page,
    profile: Optional[Dict[str, Any]] = None,
    screenshot_path: Optional[str] = None,
    use_llm_judge: bool = True,
) -> Dict[str, Any]:
    """
    Full live PRESUBMIT_REVIEW gate:
    1. Captures full-page presubmit screenshot if screenshot_path is provided.
    2. Extracts every visible field from the DOM.
    3. Runs the deterministic checklist mechanically.
    4. Runs the judge-only LLM per-field evaluation against profile.json + resume
       (catches new wrong/ungrounded answers the checklist does not know about).
    5. Never modifies the page; returns pass/fail + per-field verdicts.
    """
    if screenshot_path:
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
        except Exception as e:
            logger.warning(f"Presubmit screenshot error: {e}")

    fields = await extract_dom_fields_for_review(page)
    result = run_presubmit_review(fields, profile=profile)
    result["screenshot_path"] = screenshot_path
    result["llm_verdicts"] = []

    if use_llm_judge and fields:
        llm_verdicts = await llm_judge_fields_against_record(fields, profile=profile)
        if llm_verdicts:
            result["llm_verdicts"] = llm_verdicts
            det_by_field = {v.get("field", ""): v for v in result.get("verdicts", [])}
            for lv in llm_verdicts:
                v_status = str(lv.get("verdict", "")).upper().strip()
                if v_status in ("FAIL", "UNCERTAIN"):
                    f_name = lv.get("field", "")
                    det_v = det_by_field.get(f_name)
                    # Do not let a small-model LLM hallucination override a field that already passed a specific
                    # deterministic canonical/grounding rule (e.g. grounded Why-company essay, start date +-4d, exact URL).
                    if det_v and det_v.get("verdict") == "PASS" and det_v.get("reason") not in (
                        "Verified against canonical checklist",
                        "Optional field left blank",
                    ):
                        logger.info(
                            f"Presubmit review: keeping deterministic PASS for '{f_name}' ({det_v.get('reason')}) "
                            f"over LLM judge {v_status} ({lv.get('reason')})"
                        )
                        continue
                    fail_entry = {
                        "field": f_name,
                        "value": lv.get("value", ""),
                        "verdict": v_status,
                        "reason": f"[LLM Judge] {lv.get('reason', 'Ungrounded or contradictory value')}",
                    }
                    result["failures"].append(fail_entry)
            if result["failures"]:
                result["passed"] = False
                result["decision"] = "FAIL"
                result["halt_code"] = "PRESUBMIT_REVIEW_FAILED"
                first = result["failures"][0]
                result["summary"] = (
                    f"PRESUBMIT_REVIEW_FAILED ({len(result['failures'])}/{len(fields)} failed): "
                    f"field='{first['field']}', value='{first['value']}', verdict={first['verdict']}, reason={first['reason']}"
                )

    return result
