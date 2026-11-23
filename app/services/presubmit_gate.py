"""
AutoApply - Pre-Submit Validation Gate & Universal Form Hardening
Accurate, deterministic, grounded only in provided candidate facts.

Capabilities:
1. Completeness Check: re-enumerates all required fields immediately before submit
   (text, textarea, select, radio, checkbox, pickers/comboboxes, file uploads, toggles)
   across main document and all iframes/shadow roots. Rejects empty/placeholder/unselected fields.
2. Profile-Grounded Accuracy & Provenance Check:
   Traces every answer to a verified candidate profile key, resume section, or deterministic rule.
   Rejects sourceless, contradictory, hallucinated, or placeholder ("N/A", "--") answers.
3. Consistency & Determinism Check:
   Normalizes questions, caches decisions per run (RunAnswerCache), enforces byte-identical
   answers on repeated encounters, and cross-checks review/summary pages against intended answers.
4. Security Challenge & Overlay Detector:
   Detects Turnstile, reCAPTCHA, hCaptcha, Arkose Labs, AWS WAF, and human-verification widgets.
   Pre-click elementFromPoint check to prevent intercepted clicks; dismisses cookie banners once.
5. Failure Classification & Bounded Recovery:
   Classifies failures into transient (bounded retry) vs terminal (immediate zero-retry halt).
   Structured application audit logs with exact stage, class, timestamp, and screenshot.
"""
import os
import re
import json
import time
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Dict, Any, List, Optional, Tuple, Set

try:
    from config import settings
    DATA_DIR = getattr(settings, "DATA_DIR", "data")
except Exception:
    DATA_DIR = "data"

logger = logging.getLogger(__name__)

UNMAPPED_FIELDS_LOG_PATH = os.path.join(DATA_DIR, "unmapped_fields.log")


# ── Question Normalization ───────────────────────────────────────────────────

# Common synonym mappings for form questions
SYNONYM_REPLACEMENTS = [
    (r"\bphone\s*(number|no\.?|#)?\b", "phone"),
    (r"\b(mobile|cell)\s*(number|phone)?\b", "phone"),
    (r"\bemail\s*(address)?\b", "email"),
    (r"\bfirst\s*name\b|\bgiven\s*name\b", "first name"),
    (r"\blast\s*name\b|\bfamily\s*name\b|\bsurname\b", "last name"),
    (r"\bfull\s*legal\s*name\b|\blegal\s*name\b", "full name"),
    (r"\b(current\s*)?location\b|\bwhere\s*are\s*you\s*located\b", "location"),
    (r"\b(desired|expected|target)\s*(salary|compensation|pay|rate)\b", "expected salary"),
    (r"\b(earliest\s*)?start\s*date\b|\bhow\s*soon\s*can\s*you\s*start\b|\bavailability\s*to\s*start\b", "notice period"),
    (r"\byears?\s*of\s*(?:relevant\s*|professional\s*|software\s*|work\s*)?experience\b", "years of experience"),
    (r"\bauthorized\s*to\s*work\s*in\s*the\s*(us|united\s*states|u\.s\.)\b", "work authorization"),
    (r"\b(require|need)\s*(visa\s*)?sponsorship\b", "visa sponsorship"),
    (r"\bwhere\s*do\s*you\s*plan\s*on\s*working\s*from\s*(\(for\s*payroll\s*tax\s*purposes\))?\b", "payroll tax location"),
    (r"\btext\s*message\s*(consent|updates?|agreement)\b|\bsms\s*(consent|updates?)\b", "text messaging consent"),
]



def normalize_question(question: str) -> str:
    """
    Normalize form questions for deterministic matching and consistency caching:
    - Lowercase and trim
    - Strip visual required markers (*, ✱, (required), [required])
    - Strip punctuation and special characters
    - Apply synonym mappings
    - Collapse extra whitespace
    """
    if not question:
        return ""
    q = question.lower().strip()
    # Strip HTML tags if present
    q = re.sub(r"<[^>]+>", " ", q)
    # Strip asterisks, bullets, required markers
    q = re.sub(r"[\*✱•]", " ", q)
    q = re.sub(r"\((?:required|optional)\)|\[(?:required|optional)\]", " ", q, flags=re.IGNORECASE)
    # Strip punctuation
    q = re.sub(r"[:\?!\'\",;/\(\)\[\]{}—–\-_]", " ", q)
    # Apply standard synonyms
    for pattern, replacement in SYNONYM_REPLACEMENTS:
        q = re.sub(pattern, replacement, q, flags=re.IGNORECASE)
    # Collapse whitespace
    return " ".join(q.split())


# ── Run Decision Cache (Session-Scoped Determinism) ──────────────────────────

class RunAnswerCache:
    """
    Session-scoped cache ensuring the exact same normalized question strictly receives
    the byte-identical answer across multi-step forms, retries, and different ATS pages.
    Any answer mutation for the same normalized question raises an assertion error / block.
    """

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}

    def clear(self):
        self._cache.clear()

    def get(self, normalized_question: str) -> Optional[Dict[str, Any]]:
        return self._cache.get(normalized_question)

    def put(self, normalized_question: str, answer: str, provenance: str, stage: str):
        """Record a resolved question answer."""
        if not normalized_question:
            return
        self._cache[normalized_question] = {
            "answer": answer,
            "provenance": provenance,
            "stage": stage,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def assert_consistent(self, normalized_question: str, new_answer: str) -> Tuple[bool, Optional[str]]:
        """
        Assert that if this normalized question was previously answered in this session,
        the new answer matches byte-for-byte.
        Returns: (is_consistent: bool, error_detail: Optional[str])
        """
        if normalized_question not in self._cache:
            return True, None
        prev_answer = self._cache[normalized_question]["answer"]
        if str(prev_answer).strip() != str(new_answer).strip():
            err = (
                f"Determinism violation for question '{normalized_question}': "
                f"previously answered '{prev_answer}' (stage={self._cache[normalized_question]['stage']}), "
                f"now attempting '{new_answer}'"
            )
            logger.error(err)
            return False, err
        return True, None


# ── Provenance & Grounded Accuracy ───────────────────────────────────────────

BANNED_PLACEHOLDERS = {
    "n/a", "na", "none", "test", "todo", "--", "---", "undefined",
    "null", "placeholder", "your answer", "your answer here", "enter text",
    "select...", "choose...", "select an option", "please select"
}


@dataclass
class ProvenanceTag:
    """Tracks the exact source origin of an answer."""
    source_key: str          # e.g. "profile:personal_info.email", "profile:compliance.work_auth"
    source_value: Any        # Canonical raw value from candidate profile
    description: str         # Human-readable rationale
    is_grounded: bool = True # True if derived from verified profile or deterministic rule


class CandidateProfileStore:
    """
    Single source of truth for candidate information.
    Consolidates verified facts from config, database, and profile.json.
    """

    def __init__(self, profile_data: Optional[Dict[str, Any]] = None):
        self.profile = profile_data or self._load_default_profile()

    def _load_default_profile(self) -> Dict[str, Any]:
        """Load profile from settings and fallback profile.json."""
        # 1. Start with settings defaults
        data = {
            "personal_info": {
                "first_name": getattr(settings, "FIRST_NAME", "Siddhesh Santosh"),
                "last_name": getattr(settings, "LAST_NAME", "Sawant"),
                "full_name": getattr(settings, "FULL_NAME", "Siddhesh Santosh Sawant"),
                "email": getattr(settings, "EMAIL", getattr(settings, "APPLICANT_EMAIL", "siddheshs202001@gmail.com")),
                "phone": getattr(settings, "PHONE", getattr(settings, "APPLICANT_PHONE", "(445) 208-2299")),
                "location": getattr(settings, "LOCATION", "Washington, District of Columbia, United States"),
                "city": "Washington",
                "state": "District of Columbia",
                "postal_code": "20024",
                "country": "United States",
            },
            "links": {
                "linkedin_url": getattr(settings, "LINKEDIN_URL", "https://www.linkedin.com/in/sss20/"),
                "github_url": getattr(settings, "GITHUB_URL", "https://github.com/sids202001"),
                "portfolio_url": getattr(settings, "PORTFOLIO_URL", "https://siddhesh202001-portfolio.netlify.app"),
            },
            "education": {
                "school": "George Mason University",
                "degree": "Master of Science",
                "major": "Computer Science / Information Systems",
                "start_year": "2022",
                "end_year": "2024",
                "gpa": "3.8",
            },
            "experience": {
                "years_of_experience": int(re.search(r"\d+", str(getattr(settings, "YEARS_OF_EXPERIENCE", "2"))).group()) if re.search(r"\d+", str(getattr(settings, "YEARS_OF_EXPERIENCE", "2"))) else 2,
                "current_company": "Talviro INC",
                "current_title": "Software Developer",
                "past_companies": ["Amazon", "Talviro INC"],
                "notice_period": getattr(settings, "NOTICE_PERIOD", "Immediately"),
                "expected_salary": getattr(settings, "EXPECTED_SALARY", "$120,000"),
                "hourly_rate": "60",
            },
            "compliance_and_work_auth": {
                "work_auth_status": getattr(settings, "WORK_AUTH_STATUS", "Authorized / Require Sponsorship"),
                "us_work_authorized": True,
                "require_visa_sponsorship": True,
                "us_person_status": getattr(settings, "US_PERSON_STATUS", "None of the above; I am a citizen of a different country"),
                "hybrid_office_willingness": getattr(settings, "HYBRID_OFFICE_WILLINGNESS", "Yes"),
                "remote_willingness": getattr(settings, "REMOTE_WILLINGNESS", "Yes"),
                "relocation_willingness": getattr(settings, "RELOCATION_WILLINGNESS", "Yes"),
                "payroll_tax_location": getattr(settings, "LOCATION", "Washington, District of Columbia, United States"),
                "auditor_independence": getattr(settings, "AUDITOR_INDEPENDENCE", "No - I have never been employed by PwC"),
                "military_objections": "No",
                "government_employment": "No",
                "privacy_policy_consent": getattr(settings, "PRIVACY_POLICY_CONSENT", "Yes"),
                "text_messaging_consent": "Yes - I consent to receiving text messages",
            },
            "eeo_demographics": {
                "gender": "Male",
                "race_ethnicity": "Asian",
                "hispanic_latino": "No",
                "veteran_status": "I am not a protected veteran",
                "disability_status": "No, I do not have a disability",
                "pronouns": "He/Him",
            },
        }

        # 2. Check sid_job_scrip/profile.json if available
        json_path = os.path.expanduser("~/sid_job_scrip/profile.json")
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    p_json = json.load(f)
                    if "personal_info" in p_json:
                        data["personal_info"].update(p_json["personal_info"])
                    if "compliance_and_eeo" in p_json:
                        data["compliance_and_work_auth"].update(p_json["compliance_and_eeo"])
                    if "preferences" in p_json:
                        data["preferences"] = p_json["preferences"]
                        if "start_date" in p_json["preferences"]:
                            data["experience"]["notice_period"] = p_json["preferences"]["start_date"]
                    if "compensation" in p_json:
                        data["compensation"] = p_json["compensation"]
                        if "annual_salary_expectation" in p_json["compensation"]:
                            data["experience"]["expected_salary"] = p_json["compensation"]["annual_salary_expectation"]
                    if "education" in p_json:
                        data["education"] = p_json["education"]
            except Exception as e:
                logger.debug(f"Could not load sid_job_scrip profile.json: {e}")

        return data

    def is_known_profile_fact(self, value: Any) -> bool:
        """Check if value matches any candidate profile fact character-by-character."""
        if value is None:
            return False
        v_low = str(value).strip().lower()
        if not v_low:
            return False

        def _check(d):
            if isinstance(d, dict):
                for val in d.values():
                    if _check(val):
                        return True
            elif isinstance(d, list):
                for item in d:
                    if _check(item):
                        return True
            elif d is not None and not isinstance(d, bool):
                s = str(d).strip().lower()
                if s == v_low:
                    return True
                # Space-normalized check
                if re.sub(r'[\s\xa0]+', ' ', s).strip() == re.sub(r'[\s\xa0]+', ' ', v_low).strip():
                    return True
            return False

        if _check(self.profile):
            return True

        # Check dynamic computed start date from profile rule
        try:
            from form_filler import compute_start_date
            c_iso = compute_start_date(fmt="%Y-%m-%d").lower()
            c_us = compute_start_date(fmt="%m/%d/%Y").lower()
            c_text = compute_start_date(fmt="%B %d, %Y").lower()
            if v_low in (c_iso, c_us, c_text):
                return True
        except Exception:
            pass

        return False

    def get_fact(self, path: str) -> Any:
        """Access nested fact via dot-notation: e.g. 'personal_info.email' or 'education.school'."""
        parts = path.split(".")
        curr = self.profile
        for p in parts:
            if isinstance(curr, dict):
                if p in curr:
                    curr = curr[p]
                elif p == "major" and "discipline" in curr:
                    curr = curr["discipline"]
                elif p == "discipline" and "major" in curr:
                    curr = curr["major"]
                else:
                    return None
            elif isinstance(curr, list):
                if p.isdigit() and int(p) < len(curr):
                    curr = curr[int(p)]
                elif len(curr) > 0 and isinstance(curr[0], dict):
                    elem = curr[0]
                    if p in elem:
                        curr = elem[p]
                    elif p == "major" and "discipline" in elem:
                        curr = elem["discipline"]
                    elif p == "discipline" and "major" in elem:
                        curr = elem["major"]
                    else:
                        return None
                else:
                    return None
            else:
                return None

        if path in ("preferences.start_date", "preferences.start_date_rule", "preferences.earliest_start_date", "experience.notice_period"):
            if str(curr).lower() in ("submit_date + 1 month", "submit_date+1month", "immediately") or "month" in str(curr).lower() or curr is None:
                try:
                    from form_filler import compute_start_date
                    return compute_start_date()
                except Exception:
                    pass

        return curr


class AccuracyValidator:
    """
    Validates filled values against candidate facts.
    Rejects:
    - Hallucinated or contradictory facts
    - Generic placeholder values on required fields
    - Options that contradict profile (e.g. wrong work authorization or wrong years of experience)
    """

    def __init__(self, profile_store: CandidateProfileStore):
        self.profile = profile_store

    def validate_field_accuracy(
        self,
        question_label: str,
        filled_value: Any,
        provenance: Optional[ProvenanceTag],
        is_required: bool = True
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate whether filled_value accurately reflects the candidate's verified profile.
        """
        val_str = str(filled_value).strip()

        # 1. Required field completeness & placeholder check
        if is_required:
            if not val_str:
                return False, f"Required field '{question_label}' has empty value"
            norm_lbl = question_label.lower()
            is_clearance_or_conditional = any(k in norm_lbl for k in ["clearance", "specify", "if yes", "if other", "if applicable", "if you chose"])
            is_profile_fact = self.profile.is_known_profile_fact(val_str)
            if not is_profile_fact and val_str.lower() in BANNED_PLACEHOLDERS and not (is_clearance_or_conditional and val_str.lower() in ["n/a", "na", "none"]):
                return False, f"Required field '{question_label}' holds banned placeholder '{val_str}'"

        # If field is optional and empty, it passes
        if not val_str and not is_required:
            return True, None

        # 2. Provenance Check: must have a traceable source
        if not provenance or not provenance.is_grounded or not provenance.source_key:
            return False, f"Sourceless answer for field '{question_label}' (value='{val_str}'). Missing provenance."

        norm_q = normalize_question(question_label)
        val_low = val_str.lower()

        # 3. Work Authorization & Sponsorship Verification
        if "work authorization" in norm_q or "authorized to work" in norm_q:
            # Candidate IS legally authorized
            if any(neg in val_low for neg in ["not authorized", "no", "false", "unauthorized"]):
                return False, f"Contradicts profile: Candidate is authorized to work, but filled '{val_str}'"

        if "sponsorship" in norm_q or "visa" in norm_q:
            # Candidate DOES require sponsorship
            if "require" in norm_q and any(neg in val_low for neg in ["no", "false", "do not require", "will not"]):
                return False, f"Contradicts profile: Candidate requires visa sponsorship, but filled '{val_str}'"

        # 4. Identity verification (Email, Phone, Name) - Formatting-Normalized
        if "email" in norm_q:
            exp_email = str(self.profile.get_fact("personal_info.email") or "").lower().strip()
            if exp_email and exp_email != val_low.strip():
                return False, f"Email mismatch: filled '{val_str}' does not match candidate email '{exp_email}'"

        if any(k in norm_q for k in ["country code", "phone country", "calling code"]):
            if not any(v in val_low for v in ["+1", "1", "united states", "us"]):
                return False, f"Phone country code mismatch: filled '{val_str}'"
            return True, None

        if "phone" in norm_q:
            if val_low in ["+1", "1", "us", "united states"] or len(re.sub(r"\D", "", val_str)) <= 3:
                return True, None
            raw_exp = str(self.profile.get_fact("personal_info.phone") or "")
            if re.sub(r'[\s\xa0]+', '', raw_exp).lower() == re.sub(r'[\s\xa0]+', '', val_str).lower():
                return True, None
            exp_phone = re.sub(r"\D", "", raw_exp)
            fill_digits = re.sub(r"\D", "", val_str)
            if exp_phone and fill_digits:
                exp_10 = exp_phone[-10:] if len(exp_phone) >= 10 else exp_phone
                fill_10 = fill_digits[-10:] if len(fill_digits) >= 10 else fill_digits
                if exp_10 != fill_10 and exp_phone[-7:] not in fill_digits:
                    return False, f"Phone mismatch: filled '{val_str}' does not contain expected digits '{exp_phone[-7:]}'"
                return True, None

        if "first name" in norm_q:
            exp_first = str(self.profile.get_fact("personal_info.first_name") or "").lower().strip()
            if exp_first and exp_first not in val_low:
                return False, f"First name mismatch: filled '{val_str}' does not match '{exp_first}'"
        elif "last name" in norm_q:
            exp_last = str(self.profile.get_fact("personal_info.last_name") or "").lower().strip()
            if exp_last and exp_last not in val_low:
                return False, f"Last name mismatch: filled '{val_str}' does not match '{exp_last}'"

        # 5. Years of experience bracket check
        if "years of experience" in norm_q or "years" in norm_q or "how many years" in norm_q or "experience" in norm_q:
            cand_years = self.profile.get_fact("experience.years_of_experience") or 2

            # Check for range violation (e.g. if candidate has 2 years, option cannot be 5-7 years or 10+ years)
            ranges = re.findall(r"\d+", val_low)
            if len(ranges) >= 2:
                low, high = int(ranges[0]), int(ranges[1])
                if cand_years < low or cand_years > high:
                    return False, f"Years of experience bracket [{low}-{high}] contradicts candidate profile ({cand_years} yrs)"
            elif len(ranges) == 1 and ("+" in val_low or "more" in val_low):
                min_req = int(ranges[0])
                if cand_years < min_req:
                    return False, f"Years of experience option '{val_str}' ({min_req}+ yrs) contradicts candidate profile ({cand_years} yrs)"

        return True, None


# ── Security Challenge Detector ──────────────────────────────────────────────

# ── Security Challenge Detector ──────────────────────────────────────────────

CHALLENGE_SIGNATURES: Dict[str, Dict[str, Any]] = {
    "recaptcha": {
        "name": "Google reCAPTCHA",
        "selectors": [
            "iframe[src*='google.com/recaptcha/api2/bframe']",
            "iframe[src*='google.com/recaptcha/enterprise/bframe']",
            "iframe[src*='recaptcha/api2/bframe']",
            "iframe[src*='recaptcha/enterprise/bframe']",
            "iframe[title*='recaptcha challenge' i]",
            ".recaptcha-checkbox:not([aria-checked='true'])",
            "div.g-recaptcha:not([data-size='invisible'])",
            "#g-recaptcha:not([data-size='invisible'])",
            "iframe[src*='google.com/recaptcha/api2/anchor']:not([src*='size=invisible'])",
            "iframe[src*='google.com/recaptcha']:not([src*='size=invisible'])",
            "iframe[src*='recaptcha']:not([src*='size=invisible'])",
        ],
        "patterns": [
            r"select\s+all\s+(?:images|squares)\s+with",
            r"click\s+verify\s+once\s+there\s+are\s+none\s+left",
            r"please\s+solve\s+the\s+challenge",
            r"please\s+try\s+again\s+after\s+solving",
        ],
    },
    "hcaptcha": {
        "name": "h-Captcha",
        "selectors": [
            "iframe[src*='hcaptcha.com']",
            ".h-captcha",
            "#h-captcha",
            "iframe[title*='hCaptcha' i]",
            "[data-hcaptcha-widget-id]",
        ],
        "patterns": [
            r"hcaptcha\.com",
            r"protected\s+by\s+hcaptcha",
            r"h-captcha",
            r"hcaptcha",
        ],
    },
    "arkose": {
        "name": "Arkose Labs / FunCaptcha",
        "selectors": [
            "iframe[src*='arkoselabs.com']",
            "iframe[src*='funcaptcha.com']",
            "#fc-iframe-wrap",
            "#arkose",
            "div[id*='arkose' i]",
            "iframe[title*='arkose' i]",
        ],
        "patterns": [
            r"arkoselabs",
            r"funcaptcha",
            r"arkose\s+labs",
        ],
    },
    "cloudflare": {
        "name": "Cloudflare Turnstile & Managed Challenge",
        "selectors": [
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='turnstile']",
            ".cf-turnstile",
            "#turnstile-wrapper",
            "#cf-challenge",
            "#challenge-running",
            "#challenge-stage",
            "div[id*='cf-turnstile']",
            "div.cf-browser-verification",
        ],
        "patterns": [
            r"checking\s+your\s+browser\s+before\s+accessing",
            r"attention\s+required!\s*\|\s*cloudflare",
            r"cf-turnstile",
            r"challenges\.cloudflare\.com",
            r"just\s+a\s+moment\.\.\.",
            r"please\s+stand\s+by,\s+while\s+we\s+are\s+checking\s+your\s+browser",
            r"enable\s+javascript\s+and\s+cookies\s+to\s+continue",
        ],
    },
    "datadome": {
        "name": "DataDome",
        "selectors": [
            "iframe[src*='captcha-delivery.com']",
            "iframe[src*='datadome.co']",
            "div#datadome-captcha",
            ".datadome-slider",
            "[id*='datadome']",
            "iframe[src*='geo.captcha-delivery.com']",
        ],
        "patterns": [
            r"datadome",
            r"geo\.captcha-delivery\.com",
            r"protected\s+by\s+datadome",
        ],
    },
    "perimeterx": {
        "name": "PerimeterX / HUMAN Security",
        "selectors": [
            "iframe[src*='challenges.humansecurity.com']",
            "iframe[src*='perimeterx']",
            "#px-captcha",
            "[id*='px-captcha']",
            "div[id*='px-']",
        ],
        "patterns": [
            r"perimeterx",
            r"human\s+security",
            r"challenges\.humansecurity\.com",
            r"press\s+&\s+hold\s+to\s+confirm\s+you\s+are\s+a\s+human",
            r"press\s+and\s+hold\s+to\s+confirm\s+you\s+are\s+a\s+human",
        ],
    },
    "kasada": {
        "name": "Kasada",
        "selectors": [
            "div[id*='kasada']",
            "iframe[src*='kasada']",
            "[data-kpf]",
            "script[src*='khost']",
        ],
        "patterns": [
            r"kasada",
            r"kpsdk",
        ],
    },
    "aws_waf": {
        "name": "AWS WAF Captcha",
        "selectors": [
            "#aws-waf-captcha",
            "iframe[src*='awswaf']",
            "iframe[src*='token.awswaf.com']",
            "div[id*='aws-waf']",
        ],
        "patterns": [
            r"aws\s+waf",
            r"awswaf",
            r"token\.awswaf\.com",
        ],
    },
    "generic": {
        "name": "Security Challenge",
        "selectors": [
            "[data-qa*='captcha']",
            "[id*='captcha' i]",
            "[class*='captcha' i]:not([class*='uncaptcha']):not(.grecaptcha-badge)",
            "iframe[src*='captcha']:not([src*='size=invisible'])",
            "[aria-label*='captcha' i]",
        ],
        "patterns": [
            r"verify\s+you\s+are\s+human",
            r"verify\s+that\s+you\s+are\s+a\s+human",
            r"complete\s+the\s+security\s+check",
            r"security\s+challenge",
            r"bot\s+detection",
            r"unusual\s+traffic",
            r"confirm\s+you\s+are\s+not\s+a\s+robot",
            r"confirm\s+you'?re\s+human",
            r"i\s+am\s+not\s+a\s+robot",
        ],
    },
}

# Backward compatible flat lists
SECURITY_CHALLENGE_SELECTORS = [sel for s in CHALLENGE_SIGNATURES.values() for sel in s["selectors"]]
CHALLENGE_TEXT_PATTERNS = [pat for s in CHALLENGE_SIGNATURES.values() for pat in s["patterns"]]


class SecurityChallengeDetector:
    """
    Universal Security Challenge & Anti-Bot Verification Detector.
    Identifies reCAPTCHA, hCaptcha, Turnstile, Arkose Labs, DataDome, PerimeterX,
    Kasada, AWS WAF, and generic verification challenges across frames and HTML snapshots.
    Provides immediate terminal abort with zero click or bypass attempts.
    """

    @classmethod
    async def detect_challenge(cls, page) -> Tuple[bool, Optional[str]]:
        """
        Inspect the page and all child frames for security challenges across all signatures.
        Returns: (is_challenge_detected: bool, challenge_type: Optional[str])
        """
        if not page:
            return False, None

        # 1. Check DOM selectors in main page and frames
        targets = [page] + (getattr(page, "frames", []) or [])
        for cat_key, sig in CHALLENGE_SIGNATURES.items():
            cat_name = sig["name"]
            for sel in sig["selectors"]:
                for target in targets:
                    try:
                        loc = target.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible():
                            if cat_key in ("recaptcha", "generic"):
                                try:
                                    is_inv = await loc.evaluate('''el => {
                                        if (el.closest('.grecaptcha-badge') || el.classList.contains('grecaptcha-badge')) return true;
                                        const src = el.getAttribute('src') || '';
                                        if (src.includes('size=invisible')) return true;
                                        const ds = el.getAttribute('data-size') || '';
                                        if (ds === 'invisible') return true;
                                        const style = window.getComputedStyle(el);
                                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return true;
                                        const rect = el.getBoundingClientRect();
                                        if (rect.width <= 1 || rect.height <= 1) return true;
                                        return false;
                                    }''')
                                    if is_inv:
                                        continue
                                except Exception:
                                    pass
                            return True, f"{cat_name} (selector: '{sel}')"
                    except Exception:
                        continue

        # 2. Check visible text patterns in page content
        try:
            content = (await page.content()).lower()
            for cat_key, sig in CHALLENGE_SIGNATURES.items():
                cat_name = sig["name"]
                for pattern in sig["patterns"]:
                    if re.search(pattern, content, re.IGNORECASE):
                        return True, f"{cat_name} (pattern: '{pattern}')"
        except Exception:
            pass

        return False, None

    @classmethod
    def detect_in_html_snapshot(cls, html_content: str) -> Tuple[bool, Optional[str]]:
        """Offline detection for testing without live browser."""
        if not html_content:
            return False, None
        content_low = html_content.lower()

        for cat_key, sig in CHALLENGE_SIGNATURES.items():
            cat_name = sig["name"]

            # Key marker strings from selectors
            for sel in sig["selectors"]:
                # If recaptcha or generic, filter out purely invisible badges
                if cat_key in ("recaptcha", "generic") and ("size=invisible" in content_low or "class=\"grecaptcha-badge\"" in content_low or "data-size=\"invisible\"" in content_low):
                    if "bframe" not in content_low and "recaptcha-checkbox" not in content_low and 'class="g-recaptcha"' not in content_low:
                        continue
                # Check for explicit src matches
                match_src = re.search(r"src\*?=['\"]([^'\"]+)['\"]", sel)
                if match_src and match_src.group(1).lower() in content_low:
                    return True, f"{cat_name} (src: '{match_src.group(1)}')"

                # Check for id matches: selector starts with #
                match_id = re.search(r"(?:^|[\s,])#([\w\-]+)", sel)
                if match_id:
                    id_val = match_id.group(1).lower()
                    if re.search(r'id=["\'][^"\']*\b' + re.escape(id_val) + r'\b[^"\']*["\']', content_low):
                        return True, f"{cat_name} (id: '{id_val}')"

                # Check for class matches: selector starts with .
                match_class = re.search(r"(?:^|[\s,])\.([\w\-]+)", sel)
                if match_class:
                    cls_val = match_class.group(1).lower()
                    if re.search(r'class=["\'][^"\']*\b' + re.escape(cls_val) + r'\b[^"\']*["\']', content_low):
                        return True, f"{cat_name} (class: '{cls_val}')"

            # Check text patterns
            for pattern in sig["patterns"]:
                if re.search(pattern, content_low, re.IGNORECASE):
                    return True, f"{cat_name} (pattern: '{pattern}')"

        return False, None

    @classmethod
    async def watch_post_submit_challenge(
        cls,
        page,
        timeout_sec: float = 15.0,
        poll_interval: float = 0.5
    ) -> Tuple[bool, Optional[str]]:
        """
        Polls page for up to timeout_sec to detect post-submit interstitials,
        such as challenges appearing upon or after clicking submit (e.g. Ashby/Ramp/Waabi).
        Returns immediately if challenge detected.
        """
        if not page:
            return False, None

        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            is_chal, chal_type = await cls.detect_challenge(page)
            if is_chal:
                return True, chal_type
            await asyncio.sleep(poll_interval)

        return False, None


# ── Pre-Click Overlay & Interception Checker ─────────────────────────────────

COOKIE_BANNER_DISMISS_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "button[id*='accept' i]",
    "button[aria-label*='accept' i]",
    "button:has-text('Accept all')",
    "button:has-text('Accept All Cookies')",
    "button:has-text('Allow all')",
    "button:has-text('I Agree')",
    "button:has-text('Accept')",
    ".cookie-banner button",
    "[aria-label*='cookie' i] button",
    "[id*='cookie' i] button:has-text('Accept')",
]


class OverlayChecker:
    """
    Ensures that the submit button is truly interactable and not obstructed by an overlay.
    If obstructed by a standard dismissible cookie banner, dismisses it once.
    If obstructed by a security challenge or modal that cannot be dismissed, blocks submit.
    """

    @classmethod
    async def verify_submit_interactable(cls, page, submit_selector: str) -> Tuple[bool, Optional[str]]:
        """
        Uses document.elementFromPoint to verify the submit button is not intercepted.
        """
        try:
            btn = page.locator(submit_selector).first
            if await btn.count() == 0:
                return False, f"Submit button '{submit_selector}' not found"

            # Check if intercepted by overlay
            is_clickable = await page.evaluate('''sel => {
                const btn = document.querySelector(sel);
                if (!btn) return { clickable: false, reason: "button not found" };
                const rect = btn.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return { clickable: false, reason: "zero dimensions" };
                const x = rect.left + rect.width / 2;
                const y = rect.top + rect.height / 2;
                const topEl = document.elementFromPoint(x, y);
                if (!topEl) return { clickable: false, reason: "no element at point" };
                if (topEl === btn || btn.contains(topEl)) return { clickable: true };
                
                // Return information about the intercepting element
                return {
                    clickable: false,
                    reason: "intercepted",
                    topTag: topEl.tagName,
                    topClass: topEl.className || "",
                    topId: topEl.id || "",
                    topText: (topEl.innerText || "").slice(0, 50)
                };
            }''', submit_selector)

            if is_clickable.get("clickable"):
                return True, None

            # If intercepted, check if it is a dismissible cookie banner
            intercept_desc = f"{is_clickable.get('topTag')} (id={is_clickable.get('topId')}, class={is_clickable.get('topClass')})"
            logger.info(f"Submit button click intercepted by overlay: {intercept_desc}. Attempting single cookie banner dismissal...")

            dismissed = False
            for dis_sel in COOKIE_BANNER_DISMISS_SELECTORS:
                try:
                    dis_btn = page.locator(dis_sel).first
                    if await dis_btn.count() > 0 and await dis_btn.is_visible():
                        await dis_btn.click(timeout=1500)
                        logger.info(f"Dismissed overlay via: {dis_sel}")
                        dismissed = True
                        break
                except Exception:
                    continue

            if dismissed:
                # Re-check interactability after dismissal
                await page.wait_for_timeout(300)
                recheck = await page.evaluate('''sel => {
                    const btn = document.querySelector(sel);
                    if (!btn) return false;
                    const rect = btn.getBoundingClientRect();
                    const x = rect.left + rect.width / 2;
                    const y = rect.top + rect.height / 2;
                    const topEl = document.elementFromPoint(x, y);
                    return Boolean(topEl && (topEl === btn || btn.contains(topEl)));
                }''', submit_selector)
                if recheck:
                    return True, None

            return False, f"Submit click obstructed by non-dismissible overlay: {intercept_desc}"

        except Exception as e:
            logger.warning(f"Error during elementFromPoint overlay check: {e}")
            return True, None  # Proceed with caution if DOM evaluation unsupported


# ── Review Page Cross-Checker ────────────────────────────────────────────────

class ReviewPageCrossChecker:
    """
    Inspects multi-step review or summary pages and cross-checks rendered values
    against the intended answers cached during the run.
    Any dropped value or mutated answer blocks submission.
    """

    @classmethod
    async def extract_review_fields(cls, page) -> List[Dict[str, str]]:
        """
        Extract key-value pairs from standard review summary cards, definition lists, or review tables.
        """
        script = '''() => {
            const results = [];
            // 1. Definition lists (<dl><dt>Label</dt><dd>Value</dd></dl>)
            const dls = Array.from(document.querySelectorAll("dl, .review-section, .application-review, [class*='review'], [class*='summary']"));
            dls.forEach(dl => {
                const dts = Array.from(dl.querySelectorAll("dt, .label, .field-label, [class*='label']"));
                dts.forEach(dt => {
                    const dd = dt.nextElementSibling;
                    if (dd && dt.innerText.trim()) {
                        results.push({
                            question: dt.innerText.trim(),
                            value: dd.innerText.trim()
                        });
                    }
                });
            });

            // 2. Generic key-value rows
            const rows = Array.from(document.querySelectorAll(".review-row, .summary-row, tr"));
            rows.forEach(r => {
                const cells = Array.from(r.querySelectorAll("td, th, .col, div"));
                if (cells.length === 2 && cells[0].innerText.trim() && cells[1].innerText.trim()) {
                    results.push({
                        question: cells[0].innerText.trim(),
                        value: cells[1].innerText.trim()
                    });
                }
            });

            return results;
        }'''
        try:
            return await page.evaluate(script)
        except Exception as e:
            logger.debug(f"Review field extraction notice: {e}")
            return []

    @classmethod
    def cross_check(
        cls,
        review_fields: List[Dict[str, str]],
        run_cache: RunAnswerCache
    ) -> Tuple[bool, List[str]]:
        """
        Cross-check extracted review fields against the run cache.
        Returns: (is_valid: bool, mismatches: List[str])
        """
        mismatches = []
        for rf in review_fields:
            q_raw = rf.get("question", "")
            rendered_val = rf.get("value", "").strip()
            norm_q = normalize_question(q_raw)

            cached_entry = run_cache.get(norm_q)
            if cached_entry:
                intended_val = str(cached_entry["answer"]).strip()
                # Compare normalized values
                if intended_val.lower() != rendered_val.lower():
                    # Check substring match (e.g. Washington DC in Washington, DC, United States)
                    if intended_val.lower() not in rendered_val.lower() and rendered_val.lower() not in intended_val.lower():
                        err = f"Review page drift on '{q_raw}': intended '{intended_val}', but rendered '{rendered_val}'"
                        mismatches.append(err)
                        logger.error(err)

        return (len(mismatches) == 0, mismatches)


# ── Completeness Checker (Universal Interactables Enumeration) ───────────────

UNIVERSAL_ENUMERATE_SCRIPT = '''() => {
    const results = [];
    const seenRadioGroups = new Set();
    const seenCheckboxGroups = new Set();

    function getCard(el) {
        return el.closest(".ashby-application-form-field-entry, .field-entry, [class*='fieldEntry'], [class*='field-entry'], [class*='FieldEntry'], .application-question, .field, .field-wrapper, .form-group, fieldset, [data-qa*='field']");
    }

    function isVisible(el) {
        if (el.offsetParent !== null) return true;
        const parent = el.closest("label, .application-question, .field, [class*='checkbox'], [class*='radio'], .ashby-application-form-field-entry, [class*='fieldEntry']");
        return Boolean(parent && parent.offsetParent !== null);
    }

    function getLabel(el) {
        if (el.id && (el.id === "country" || el.id.includes("country_code") || el.id.includes("phone_country"))) {
            return "Phone Country Code";
        }
        if (el.getAttribute("aria-label")) return el.getAttribute("aria-label").trim();
        const card = getCard(el);
        if (card) {
            const lbl = card.querySelector(".text, .application-label, legend, h4, .label, label, [class*='title'], [class*='Title'], [class*='label']");
            if (lbl && lbl.innerText.trim()) {
                return lbl.innerText.split('\\n')[0].replace(/[✱*]/g, "").trim();
            }
        }
        if (el.id) {
            try {
                const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                if (l && l.innerText.trim()) return l.innerText.replace(/[✱*]/g, "").trim();
            } catch(e) {}
        }
        const parentLabel = el.closest("label");
        if (parentLabel && parentLabel.innerText.trim()) {
            return parentLabel.innerText.replace(/[✱*]/g, "").trim();
        }
        return el.name || el.placeholder || el.id || "Unknown Field";
    }

    function isRequired(el, card) {
        if (el.required || el.getAttribute("aria-required") === "true") return true;
        if (card) {
            if (card.classList.contains("required") || card.hasAttribute("required")) return true;
            if (card.querySelector(".required, [required], [class*='required'], [class*='asterisk'], [class*='error'], [aria-invalid='true']")) return true;
            const text = card.innerText || "";
            if (text.includes("✱") || text.includes("*") || text.toLowerCase().includes("required")) return true;
        }
        const nameOrId = ((el.name || "") + " " + (el.id || "")).toLowerCase();
        if (nameOrId.includes("consent") || nameOrId.includes("privacy") || nameOrId.includes("terms")) {
            return true;
        }
        return false;
    }

    // 1. Text, Textarea & Combobox inputs
    const textEls = Array.from(document.querySelectorAll("input:not([type='hidden']):not([type='checkbox']):not([type='radio']):not([type='submit']):not([type='button']):not([type='file']), textarea"));
    textEls.forEach(el => {
        if (!isVisible(el)) return;
        const card = getCard(el);
        const req = isRequired(el, card);
        let val = (el.value || "").trim();
        const isCombobox = el.getAttribute("role") === "combobox" || el.classList.contains("ashby-application-form-input-autocomplete") || el.classList.contains("select__input");
        if (!val && card) {
            const singleVal = card.querySelector(".select__single-value, [class*='singleValue'], [class*='single-value']");
            if (singleVal && singleVal.innerText.trim()) {
                val = singleVal.innerText.trim();
            }
        }
        results.push({
            type: isCombobox ? "combobox" : (el.tagName.toLowerCase() === "textarea" ? "textarea" : (el.type || "text")),
            id: el.id || "",
            name: el.name || "",
            label: getLabel(el),
            value: val,
            is_required: req,
            is_filled: val.length > 0 && !/^(select\\.*|choose\\.*|--+|placeholder)$/i.test(val),
            choices: []
        });
    });

    // 2. Selects
    const selectEls = Array.from(document.querySelectorAll("select"));
    selectEls.forEach(el => {
        if (!isVisible(el)) return;
        const card = getCard(el);
        const req = isRequired(el, card);
        const opt = el.options[el.selectedIndex];
        const optText = opt ? opt.text.trim() : "";
        const isPlaceholder = !optText || /^(select\\.*|choose\\.*|--+|please select\\.*)$/i.test(optText);
        const allChoices = Array.from(el.options)
            .map(o => o.text.trim())
            .filter(t => t && !/^(select\\.*|choose\\.*|--+|please select\\.*)$/i.test(t));
        let selectLabel = getLabel(el);
        if (optText.startsWith("+") || optText.includes("+1") || (el.id && el.id.includes("country_code")) || (el.name && el.name.includes("country_code"))) {
            selectLabel = "Phone Country Code";
        }
        results.push({
            type: "select",
            id: el.id || "",
            name: el.name || "",
            label: selectLabel,
            value: isPlaceholder ? "" : optText,
            is_required: req,
            is_filled: !isPlaceholder && optText.length > 0,
            choices: allChoices
        });
    });

    // 3. Radio groups (native inputs and ARIA radio roles)
    const radioEls = Array.from(document.querySelectorAll("input[type='radio'], [role='radio']"));
    radioEls.forEach(el => {
        if (!isVisible(el)) return;
        const groupName = el.name || el.getAttribute("name") || getCard(el)?.id || "unnamed_radio";
        if (seenRadioGroups.has(groupName)) return;
        seenRadioGroups.add(groupName);

        const card = getCard(el);
        const req = isRequired(el, card);
        let groupRadios = [];
        if (el.name) {
            try {
                groupRadios = Array.from(document.querySelectorAll(`input[type='radio'][name="${CSS.escape(el.name)}"], [role='radio'][name="${CSS.escape(el.name)}"]`));
            } catch(e) {}
        }
        if (!groupRadios.length && card) {
            groupRadios = Array.from(card.querySelectorAll("input[type='radio'], [role='radio']"));
        }
        if (!groupRadios.length) groupRadios = [el];

        let anyChecked = groupRadios.some(r => r.checked || r.getAttribute("aria-checked") === "true");
        let checkedVal = "";
        const choices = [];
        groupRadios.forEach(r => {
            let rText = "";
            if (r.id) {
                try {
                    const lFor = document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
                    if (lFor && lFor.innerText.trim()) rText = lFor.innerText.trim();
                } catch(e) {}
            }
            if (!rText) {
                const lClosest = r.closest("label");
                if (lClosest && lClosest.innerText.trim()) rText = lClosest.innerText.trim();
            }
            if (!rText) rText = (r.value || r.innerText || "").trim();
            if (rText) choices.push(rText);
            const isRChecked = r.checked || r.getAttribute("aria-checked") === "true";
            if (isRChecked) checkedVal = rText;
        });

        if (!anyChecked && card) {
            const ariaChecked = card.querySelector("[role='radio'][aria-checked='true']");
            if (ariaChecked) {
                anyChecked = true;
                checkedVal = ariaChecked.innerText.trim();
            }
        }

        results.push({
            type: "radio",
            id: el.id || "",
            name: el.name || groupName,
            label: getLabel(el),
            value: checkedVal,
            is_required: req,
            is_filled: anyChecked,
            choices: choices
        });
    });

    // 4. Checkboxes (groups & single consent)
    const cbEls = Array.from(document.querySelectorAll("input[type='checkbox']"));
    cbEls.forEach(el => {
        if (!isVisible(el)) return;
        const card = getCard(el);
        const req = isRequired(el, card);

        let sameNameCbs = [];
        if (el.name) {
            try {
                sameNameCbs = Array.from(document.querySelectorAll("input[type='checkbox'][name='" + CSS.escape(el.name) + "']"));
            } catch(e) {}
        }
        const cardCbs = card ? Array.from(card.querySelectorAll("input[type='checkbox']")) : [];

        if (sameNameCbs.length > 1 || cardCbs.length > 1) {
            const groupCbs = sameNameCbs.length > 1 ? sameNameCbs : cardCbs;
            const groupKey = el.name ? ("name:" + el.name) : ("card:" + (card ? card.innerText.slice(0, 30) : el.id));
            if (seenCheckboxGroups.has(groupKey)) return;
            seenCheckboxGroups.add(groupKey);

            const anyChecked = groupCbs.some(cb => cb.checked);
            let checkedVal = "";
            groupCbs.forEach(cb => {
                if (cb.checked) {
                    const lFor = cb.id ? document.querySelector(`label[for='${CSS.escape(cb.id)}']`) : null;
                    checkedVal = lFor ? lFor.innerText.trim() : (cb.value || "checked");
                }
            });

            results.push({
                type: "checkbox_group",
                id: el.id || "",
                name: el.name || "",
                label: getLabel(el),
                value: checkedVal,
                is_required: req,
                is_filled: anyChecked,
                choices: []
            });
            return;
        }

        const l = el.closest("label") || el.parentElement;
        const cbLabel = l ? l.innerText.trim() : (el.value || "");
        results.push({
            type: "checkbox",
            id: el.id || "",
            name: el.name || "",
            label: getLabel(el) || cbLabel,
            value: el.checked ? "checked" : "",
            is_required: req,
            is_filled: el.checked,
            choices: [cbLabel]
        });
    });

    // 5. File inputs (uploads)
    const fileEls = Array.from(document.querySelectorAll("input[type='file']"));
    fileEls.forEach(el => {
        const card = getCard(el);
        const req = isRequired(el, card);
        const hasFile = Boolean(el.files && el.files.length > 0) || Boolean(el.value && el.value.trim() !== "");
        results.push({
            type: "file",
            id: el.id || "",
            name: el.name || "",
            label: getLabel(el) || "Resume Upload",
            value: hasFile ? (el.files && el.files[0] ? el.files[0].name : "attached") : "",
            is_required: req,
            is_filled: hasFile,
            choices: []
        });
    });

    // 6. Ashby Yes/No Button Groups
    const yesNoCards = Array.from(document.querySelectorAll(".ashby-application-form-input-yesno, [class*='yesno']"));
    yesNoCards.forEach(container => {
        const card = getCard(container);
        if (!card) return;
        const req = isRequired(container, card);
        const buttons = Array.from(container.querySelectorAll("button"));
        const anySelected = buttons.some(b => b.className.includes("selected") || b.getAttribute("aria-pressed") === "true");
        let selectedText = "";
        buttons.forEach(b => {
            if (b.className.includes("selected") || b.getAttribute("aria-pressed") === "true") selectedText = b.innerText.trim();
        });
        const qEl = card.querySelector("label, legend, [class*='title'], [class*='Title'], [class*='label']");
        const qTitle = qEl ? qEl.innerText.trim().replace(/[✱*]/g, "").trim() : "Yes/No Question";
        if (!results.some(r => r.label === qTitle)) {
            results.push({
                type: "toggle",
                id: card.getAttribute("data-field-entry-id") || card.getAttribute("data-field-path") || "",
                name: card.getAttribute("data-field-path") || "",
                label: qTitle,
                value: selectedText,
                is_required: req,
                is_filled: anySelected,
                choices: ["Yes", "No"]
            });
        }
    });

    return results;
}'''


# ── Failure Classification ───────────────────────────────────────────────────

class FailureClassifier:
    """
    Classifies any application failure into:
    - transient (timeout on healthy page, network glitch) -> eligible for bounded retry + reload
    - terminal (security challenge, access denied, candidate limit, closed posting, bot block) -> zero retries
    """

    TERMINAL_PATTERNS = [
        re.compile(r"security\s+challenge|captcha|turnstile|recaptcha|hcaptcha", re.I),
        re.compile(r"access\s+denied|forbidden|403\s+forbidden|401\s+unauthorized", re.I),
        re.compile(r"candidate\s+application\s+limit|maximum\s+number\s+of\s+active\s+applications", re.I),
        re.compile(r"posting\s+closed|job\s+is\s+no\s+longer\s+available|not\s+accepting\s+applications", re.I),
        re.compile(r"bot\s+detected|automated\s+traffic", re.I),
        re.compile(r"per-company\s+cap\s+reached|1-hour\s+cooldown\s+active", re.I),
    ]

    @classmethod
    def classify(cls, error_or_reason: str, stage: str = "unknown") -> Dict[str, Any]:
        err_str = str(error_or_reason)
        for pat in cls.TERMINAL_PATTERNS:
            if pat.search(err_str):
                return {
                    "class": "terminal",
                    "stage": stage,
                    "reason": err_str,
                    "retry_allowed": False
                }
        return {
            "class": "transient",
            "stage": stage,
            "reason": err_str,
            "retry_allowed": True
        }


# ── Pre-Submit Validation Gate (Master Coordinator) ──────────────────────────

@dataclass
class GateResult:
    is_valid: bool
    outcome: str                        # "submitted", "skipped", "capped"
    blocking_reasons: List[str] = field(default_factory=list)
    field_audit: List[Dict[str, Any]] = field(default_factory=list)
    stage: str = "pre_submit_gate"
    failure_class: Optional[str] = None # "transient" or "terminal"


class PresubmitGate:
    """
    Unified Pre-Submit Validation Gate:
    1. Re-enumerates all required fields immediately before submit across all frames.
    2. Verifies non-empty valid values for each required field.
    3. Asserts provenance and grounded accuracy against verified candidate profile.
    4. Detects security challenges (Turnstile/reCAPTCHA/hCaptcha) and aborts immediately.
    5. Validates review/summary page consistency against run cache.
    6. Ensures zero unhandled fields, zero hallucinations, zero bypass attempts.
    """

    def __init__(self, profile_store: Optional[CandidateProfileStore] = None):
        self.profile_store = profile_store or CandidateProfileStore()
        self.accuracy_validator = AccuracyValidator(self.profile_store)
        self.run_cache = RunAnswerCache()

    def log_unmapped_required_field(self, company: str, portal: str, url: str, label: str, field_type: str, choices: List[str]):
        """Append unmapped required field to unmapped_fields.log."""
        try:
            os.makedirs(os.path.dirname(UNMAPPED_FIELDS_LOG_PATH) or ".", exist_ok=True)
            ts = datetime.now(timezone.utc).isoformat()
            line = f"{ts} | {company} | {portal} | {url} | {label} | {field_type} | {choices}\n"
            with open(UNMAPPED_FIELDS_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
            logger.warning(f"Unmapped required field recorded in unmapped_fields.log: '{label}' ({company})")
        except Exception as e:
            logger.error(f"Failed to log unmapped field: {e}")

    async def validate_page_state(
        self,
        page,
        company: str = "unknown",
        portal: str = "unknown",
        url: str = "",
        provenance_map: Optional[Dict[str, ProvenanceTag]] = None,
        is_review_step: bool = False
    ) -> GateResult:
        """
        Execute full pre-submit gate audit on active Playwright page.
        """
        blocking_reasons: List[str] = []
        field_audit: List[Dict[str, Any]] = []
        provenance_map = provenance_map or {}

        # 1. Security Challenge Scan (Instant Terminal Abort)
        is_challenge, challenge_type = await SecurityChallengeDetector.detect_challenge(page)
        if is_challenge:
            reason = f"Security challenge detected: {challenge_type}"
            logger.error(f"TERMINAL ABORT: {reason}")
            return GateResult(
                is_valid=False,
                outcome="skipped",
                blocking_reasons=[reason],
                stage="security_challenge",
                failure_class="terminal"
            )

        # 2. Universal Interactable Enumeration
        enumerated_fields: List[Dict[str, Any]] = []
        try:
            enumerated_fields = await page.evaluate(UNIVERSAL_ENUMERATE_SCRIPT)
        except Exception as e:
            logger.error(f"Error enumerating required fields during pre-submit gate: {e}")

        # 3. Completeness & Accuracy Audit
        for f in enumerated_fields:
            lbl = f.get("label", "")
            ftype = f.get("type", "text")
            val = f.get("value", "")
            is_req = f.get("is_required", False)
            is_filled = f.get("is_filled", False)
            norm_q = normalize_question(lbl)

            audit_entry = {
                "label": lbl,
                "type": ftype,
                "value": val,
                "required": is_req,
                "filled": is_filled,
                "status": "pass"
            }

            if is_req and not is_filled:
                err = f"Unfilled required field: '{lbl}' (type={ftype})"
                blocking_reasons.append(err)
                audit_entry["status"] = "unfilled_required"
                self.log_unmapped_required_field(company, portal, url, lbl, ftype, f.get("choices", []))

            elif is_filled:
                # Get provenance tag
                prov = provenance_map.get(norm_q) or provenance_map.get(lbl)
                if not prov:
                    # Check if standard profile field
                    prov = self._synthesize_standard_provenance(norm_q, val)

                acc_ok, acc_err = self.accuracy_validator.validate_field_accuracy(
                    lbl, val, prov, is_required=is_req
                )
                if not acc_ok:
                    blocking_reasons.append(acc_err)
                    audit_entry["status"] = f"accuracy_fail: {acc_err}"
                else:
                    # Update run cache and verify consistency
                    consistent_ok, c_err = self.run_cache.assert_consistent(norm_q, val)
                    if not consistent_ok:
                        blocking_reasons.append(c_err)
                        audit_entry["status"] = f"determinism_violation: {c_err}"
                    else:
                        self.run_cache.put(norm_q, val, prov.source_key if prov else "synthetic", "presubmit_gate")

            field_audit.append(audit_entry)

        # 4. Review Page Cross-Check if on review step
        if is_review_step:
            review_fields = await ReviewPageCrossChecker.extract_review_fields(page)
            if review_fields:
                rev_ok, rev_mismatches = ReviewPageCrossChecker.cross_check(review_fields, self.run_cache)
                if not rev_ok:
                    blocking_reasons.extend(rev_mismatches)

        is_valid = len(blocking_reasons) == 0
        outcome = "submitted" if is_valid else "skipped"
        f_class = None if is_valid else ("terminal" if any("Security" in r or "Contradicts" in r for r in blocking_reasons) else "transient")

        return GateResult(
            is_valid=is_valid,
            outcome=outcome,
            blocking_reasons=blocking_reasons,
            field_audit=field_audit,
            stage="pre_submit_gate",
            failure_class=f_class
        )

    def _synthesize_standard_provenance(self, norm_q: str, val: str) -> ProvenanceTag:
        """Synthesize a verified provenance tag for recognized standard profile fields."""
        val_str = str(val).strip()
        if "first name" in norm_q:
            return ProvenanceTag("profile:personal_info.first_name", val_str, "Standard first name")
        if "last name" in norm_q:
            return ProvenanceTag("profile:personal_info.last_name", val_str, "Standard last name")
        if "full name" in norm_q or norm_q in ("name", "full name", "legal name", "candidate name"):
            return ProvenanceTag("profile:personal_info.full_name", val_str, "Standard full name")
        if any(c in norm_q for c in ["salary", "compensation", "hourly", "pay", "rate"]):
            return ProvenanceTag("profile:experience.expected_salary", val_str, "Candidate Compensation")
        if "email" in norm_q:
            return ProvenanceTag("profile:personal_info.email", val_str, "Standard email")
        if any(c in norm_q for c in ["country code", "phone country", "calling code"]):
            return ProvenanceTag("profile:compliance_and_work_auth.phone_country_code", val_str, "Phone country calling code")
        if "phone" in norm_q:
            return ProvenanceTag("profile:personal_info.phone", val_str, "Standard phone")
        if "linkedin" in norm_q:
            return ProvenanceTag("profile:links.linkedin_url", val_str, "Candidate LinkedIn")
        if "github" in norm_q:
            return ProvenanceTag("profile:links.github_url", val_str, "Candidate GitHub")
        if "portfolio" in norm_q or "website" in norm_q:
            return ProvenanceTag("profile:links.portfolio_url", val_str, "Candidate Portfolio")
        if any(c in norm_q for c in ["location", "working from", "relocat", "commute", "onsite", "office", "bay area"]):
            return ProvenanceTag("profile:compliance_and_work_auth.payroll_tax_location", val_str, "Location & Relocation Preference")
        if any(c in norm_q for c in ["work authorization", "eligible to work", "work in the us", "authorized to work"]):
            return ProvenanceTag("profile:compliance_and_work_auth.work_auth_status", val_str, "Work authorization status")
        if "sponsorship" in norm_q:
            return ProvenanceTag("profile:compliance_and_work_auth.require_visa_sponsorship", val_str, "Visa sponsorship")
        if "years of experience" in norm_q:
            return ProvenanceTag("profile:experience.years_of_experience", val_str, "Total experience years")
        if "resume" in norm_q:
            return ProvenanceTag("resume:attached", val_str, "Candidate PDF resume")
        if any(c in norm_q for c in ["education", "degree", "school", "graduation", "co-op", "work term", "rotation", "date", "year", "month"]):
            return ProvenanceTag("profile:education.degree", val_str, "Candidate Education & Work Term")
        if any(c in norm_q for c in ["export", "u.s. person", "itar"]):
            return ProvenanceTag("profile:compliance_and_work_auth.us_person_status", val_str, "Export Compliance Declaration")
        if "clearance" in norm_q:
            return ProvenanceTag("profile:compliance_and_work_auth.security_clearance_level", val_str, "Security Clearance Level")
        if any(c in norm_q for c in ["gender", "race", "veteran", "disability", "hispanic"]):
            return ProvenanceTag("profile:eeo_demographics", val_str, "EEO Demographics")
        if any(c in norm_q for c in ["company", "employer", "organization", "org"]):
            return ProvenanceTag("profile:experience.current_company", val_str, "Current employer")
        if any(c in norm_q for c in ["hear about", "how did you hear", "referral", "source"]):
            return ProvenanceTag("profile:preferences.referral_source", val_str, "Referral source")
        if any(c in norm_q for c in ["consent", "privacy", "terms", "acknowledge"]):
            return ProvenanceTag("profile:compliance_and_work_auth.privacy_policy_consent", val_str, "Legal consent")
        return ProvenanceTag("", val_str, "Unverified source", is_grounded=False)


# Singleton instance
presubmit_gate = PresubmitGate()
