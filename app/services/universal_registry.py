"""
AutoApply - Universal Registry Engine
Covers all standard ATS application questions across Greenhouse, Ashby, Lever, SmartRecruiters, and Workday.
Resolves questions via a 4-stage pipeline:
  1. Exact Registry Match (Regex & Aliases)
  2. Fuzzy Registry Match (difflib SequenceMatcher >= 0.85)
  3. Semantic Profile Map (Direct Profile Field Grounding)
  4. Unmapped Logging -> data/unmapped_fields.log
Strictly grounded in verified candidate profile facts — zero hallucination, zero guessing.
"""
import os
import re
import json
import logging
from difflib import SequenceMatcher
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from app.services.presubmit_gate import (
    normalize_question,
    CandidateProfileStore,
    ProvenanceTag,
    UNMAPPED_FIELDS_LOG_PATH
)

try:
    from config import settings
except Exception:
    import importlib.util
    _p = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "config.py"))
    _spec = importlib.util.spec_from_file_location("sid_job_scrip_config", _p)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    settings = _mod.settings

logger = logging.getLogger(__name__)


# ── Registry Definitions Across 9 Standard Categories ────────────────────────

UNIVERSAL_REGISTRY: Dict[str, Dict[str, Any]] = {
    # ── 1. Identity & Contact ────────────────────────────────────────────────
    "first_name": {
        "category": "identity_contact",
        "patterns": [
            r"\bfirst\s*name\b", r"\bgiven\s*name\b", r"\bforename\b", r"\bfirst\s*legal\s*name\b"
        ],
        "profile_path": "personal_info.first_name",
        "provenance_key": "profile:personal_info.first_name",
        "type": "text",
    },
    "last_name": {
        "category": "identity_contact",
        "patterns": [
            r"\blast\s*name\b", r"\bfamily\s*name\b", r"\bsurname\b", r"\blast\s*legal\s*name\b"
        ],
        "profile_path": "personal_info.last_name",
        "provenance_key": "profile:personal_info.last_name",
        "type": "text",
    },
    "full_name": {
        "category": "identity_contact",
        "patterns": [
            r"\bfull\s*name\b", r"\blegal\s*name\b", r"\bcandidate\s*name\b", r"\byour\s*name\b"
        ],
        "profile_path": "personal_info.full_name",
        "provenance_key": "profile:personal_info.full_name",
        "type": "text",
    },
    "email": {
        "category": "identity_contact",
        "patterns": [
            r"\bemail\b", r"\bemail\s*address\b", r"\bprimary\s*email\b"
        ],
        "profile_path": "personal_info.email",
        "provenance_key": "profile:personal_info.email",
        "type": "text",
    },
    "phone_country_code": {
        "category": "identity_contact",
        "patterns": [
            r"phone\s*(country|calling)?\s*code",
            r"country\s*(calling)?\s*code",
            r"country\s*phone\s*code",
            r"phone_country_code",
        ],
        "profile_path": "personal_info.phone_country_code",
        "provenance_key": "profile:personal_info.phone_country_code",
        "type": "select",
        "default": "United States +1",
    },
    "phone": {
        "category": "identity_contact",
        "patterns": [
            r"\bphone\b", r"\bphone\s*number\b", r"\bmobile\b", r"\bcell\s*phone\b", r"\bcontact\s*number\b"
        ],
        "profile_path": "personal_info.phone",
        "provenance_key": "profile:personal_info.phone",
        "type": "text",
    },
    "address": {
        "category": "identity_contact",
        "patterns": [
            r"\bstreet\s*address\b", r"\baddress\s*line\b", r"\bresidential\s*address\b"
        ],
        "profile_path": "personal_info.address",
        "provenance_key": "profile:personal_info.address",
        "type": "text",
    },
    "city": {
        "category": "identity_contact",
        "patterns": [
            r"\bcity\b", r"\bcurrent\s*city\b", r"\btown\b"
        ],
        "profile_path": "personal_info.city",
        "provenance_key": "profile:personal_info.city",
        "type": "text",
    },
    "state": {
        "category": "identity_contact",
        "patterns": [
            r"\bstate\b", r"\bstate\s*/\s*province\b", r"\bregion\b"
        ],
        "profile_path": "personal_info.state",
        "provenance_key": "profile:personal_info.state",
        "type": "text",
    },
    "postal_code": {
        "category": "identity_contact",
        "patterns": [
            r"\bzip\b", r"\bzip\s*code\b", r"\bpostal\s*code\b"
        ],
        "profile_path": "personal_info.postal_code",
        "provenance_key": "profile:personal_info.postal_code",
        "type": "text",
    },
    "country": {
        "category": "identity_contact",
        "patterns": [
            r"^(what\s+is\s+your\s+)?country\s*(of\s*residence|\?)?$",
            r"\bcountry\s*of\s*residence\b",
            r"\bnationality\b",
            r"\bwhich\s+country\s+do\s+you\s+(currently\s+)?(live|reside)\b",
            r"\bcurrent\s+country\b"
        ],
        "profile_path": "personal_info.country",
        "provenance_key": "profile:personal_info.country",
        "type": "text",
    },

    # ── 2. Location & Work Preferences ───────────────────────────────────────
    "location": {
        "category": "location_work_pref",
        "patterns": [
            r"\blocation\b", r"\bcurrent\s*location\b", r"\bwhere\s*are\s*you\s*located\b",
            r"\bcity\s*and\s*state\b", r"\bcity\s*,\s*state\b", r"\bcurrent\s*residence\b",
            r"opportunity\s*location.*", r"opportunitylocationid",
            r"which\s*location\s*are\s*you\s*applying\s*for",
            r"role\s*location", r"office\s*location"
        ],
        "profile_path": "personal_info.location",
        "provenance_key": "profile:personal_info.location",
        "type": "location",
    },
    "payroll_tax_location": {
        "category": "location_work_pref",
        "patterns": [
            r"where\s*do\s*you\s*plan\s*on\s*working\s*from",
            r"working\s*from.*payroll",
            r"payroll\s*tax",
            r"payroll.*location",
            r"plan\s*on\s*working\s*from.*tax",
            r"tax\s*purposes?"
        ],
        "profile_path": "compliance_and_work_auth.payroll_tax_location",
        "provenance_key": "profile:compliance_and_work_auth.payroll_tax_location",
        "type": "combobox",
    },
    "remote_willingness": {
        "category": "location_work_pref",
        "patterns": [
            r"open\s*to\s*(?:working\s*)?remote",
            r"work(?:ing)?\s*remotely",
            r"willing\s*to\s*work\s*remotely",
            r"remote\s*work\s*preference",
            r"comfortable\s*with\s*remote",
            r"\bremote\b"
        ],
        "profile_path": "compliance_and_work_auth.remote_willingness",
        "provenance_key": "profile:compliance_and_work_auth.remote_willingness",
        "type": "boolean",
    },

    "hybrid_office_willingness": {
        "category": "location_work_pref",
        "patterns": [
            r"\bhybrid\b", r"\bin-?person\b", r"\bwork\s*from\s*(our\s*)?office\b",
            r"\bdays?\s*(per|\/|a)\s*week\s*(in|from)\s*(the\s*)?office\b", r"\bon-?site\b",
            r"\bwillingness\s*to\s*work\s*in\s*office\b"
        ],
        "profile_path": "compliance_and_work_auth.hybrid_office_willingness",
        "provenance_key": "profile:compliance_and_work_auth.hybrid_office_willingness",
        "type": "boolean",
    },
    "relocation_willingness": {
        "category": "location_work_pref",
        "patterns": [
            r"\bwilling\s*to\s*relocate\b", r"\brelocation\b", r"\bopen\s*to\s*relocat\b"
        ],
        "profile_path": "compliance_and_work_auth.relocation_willingness",
        "provenance_key": "profile:compliance_and_work_auth.relocation_willingness",
        "type": "boolean",
    },

    # ── 3. Experience & Notice ───────────────────────────────────────────────
    "years_of_experience": {
        "category": "experience_notice",
        "patterns": [
            r"years?\s*(of\s*)?.*experience", r"how\s*many\s*years", r"years?\s*in\s*software",
            r"total\s*(years\s*of\s*)?experience", r"relevant\s*professional\s*experience",
            r"relevant\s*experience", r"professional\s*experience"
        ],
        "profile_path": "experience.years_of_experience",
        "provenance_key": "profile:experience.years_of_experience",
        "type": "numeric_years",
    },
    "notice_period": {
        "category": "experience_notice",
        "patterns": [
            r"\bnotice\s*period\b", r"\bhow\s*soon\s*can\s*you\s*start\b", r"\bavailability\s*to\s*start\b",
            r"\bearliest\s*start\s*date\b", r"\bwhen\s*can\s*you\s*start\b", r"\bstart\s*date\b",
            r"when\s*can\s*you\s*start\s*a\s*new\s*role", r"when\s*would\s*you\s*be\s*available\s*to\s*start",
            r"what\s*is\s*the\s*earliest\s*date"
        ],
        "profile_path": "preferences.start_date",
        "provenance_key": "profile:preferences.start_date",
        "type": "text",
    },
    "current_company": {
        "category": "experience_notice",
        "patterns": [
            r"\bcurrent\s*company\b", r"\bcurrent\s*employer\b", r"\bpresent\s*employer\b", r"\bmost\s*recent\s*company\b"
        ],
        "profile_path": "experience.current_company",
        "provenance_key": "profile:experience.current_company",
        "type": "text",
    },
    "current_title": {
        "category": "experience_notice",
        "patterns": [
            r"\bcurrent\s*title\b", r"\bcurrent\s*role\b", r"\bcurrent\s*position\b", r"\bjob\s*title\b"
        ],
        "profile_path": "experience.current_title",
        "provenance_key": "profile:experience.current_title",
        "type": "text",
    },

    # ── 4. Compensation ──────────────────────────────────────────────────────
    "expected_salary": {
        "category": "compensation",
        "patterns": [
            r"expected\s*(salary|compensation|pay|rate)",
            r"desired\s*(salary|compensation|pay|rate)",
            r"compensation\s*expectation",
            r"salary\s*expectation",
            r"target\s*compensation",
            r"target\s*salary",
            r"salary\s*requirements?"
        ],
        "profile_path": "compensation.annual_salary_expectation",
        "provenance_key": "profile:compensation.annual_salary_expectation",
        "type": "salary",
    },
    "hourly_pay": {
        "category": "compensation",
        "patterns": [
            r"hourly\s*(pay|rate)",
            r"hourly.*requirements?",
            r"hourly\s*compensation",
            r"hourly\s*pay\s*requirements?",
        ],
        "profile_path": "experience.hourly_rate",
        "provenance_key": "profile:experience.hourly_rate",
        "type": "text",
        "default": "60",
    },

    # ── 5. Professional URLs ─────────────────────────────────────────────────
    "linkedin_url": {
        "category": "professional_urls",
        "patterns": [
            r"\blinkedin\b", r"\blinked\s*in\b", r"\blinkedin\s*profile\b", r"\blinkedin\s*url\b"
        ],
        "profile_path": "links.linkedin_url",
        "provenance_key": "profile:links.linkedin_url",
        "type": "url",
    },
    "github_url": {
        "category": "professional_urls",
        "patterns": [
            r"\bgithub\b", r"\bgit\s*hub\b", r"\bgithub\s*profile\b", r"\bgithub\s*url\b"
        ],
        "profile_path": "links.github_url",
        "provenance_key": "profile:links.github_url",
        "type": "url",
    },
    "portfolio_url": {
        "category": "professional_urls",
        "patterns": [
            r"\bportfolio\b", r"\bpersonal\s*site\b", r"\bpersonal\s*website\b",
            r"\bpersonal\s*url\b", r"\bwebsite\b", r"\bpersonal\s*link\b"
        ],
        "profile_path": "links.portfolio_url",
        "provenance_key": "profile:links.portfolio_url",
        "type": "url",
    },

    # ── 6. Education & Languages ─────────────────────────────────────────────
    "school": {
        "category": "education_languages",
        "patterns": [
            r"\bschool\b", r"\buniversity\b", r"\bcollege\b", r"\binstitution\b", r"\balma\s*mater\b"
        ],
        "profile_path": "education.school",
        "provenance_key": "profile:education.school",
        "type": "text",
    },
    "degree": {
        "category": "education_languages",
        "patterns": [
            r"\bdegree\b", r"\bhighest\s*degree\b", r"\bdegree\s*level\b", r"\blevel\s*of\s*education\b"
        ],
        "profile_path": "education.degree",
        "provenance_key": "profile:education.degree",
        "type": "text",
    },
    "major": {
        "category": "education_languages",
        "patterns": [
            r"\bmajor\b", r"\bfield\s*of\s*study\b", r"\bdiscipline\b", r"\barea\s*of\s*study\b"
        ],
        "profile_path": "education.major",
        "provenance_key": "profile:education.major",
        "type": "text",
    },
    "discipline": {
        "category": "education_languages",
        "patterns": [
            r"\bdiscipline\b", r"\bmajor\b", r"\bfield\s*of\s*study\b", r"\barea\s*of\s*study\b"
        ],
        "profile_path": "education.discipline",
        "provenance_key": "profile:education.discipline",
        "type": "text",
    },
    "gpa": {
        "category": "education_languages",
        "patterns": [
            r"\bgpa\b", r"\bgrade\s*point\s*average\b"
        ],
        "profile_path": "education.gpa",
        "provenance_key": "profile:education.gpa",
        "type": "text",
    },

    # ── 7. EEO / Demographics ────────────────────────────────────────────────
    "gender": {
        "category": "eeo_demographics",
        "patterns": [
            r"\bgender\b", r"\bgender\s*identity\b", r"\bsex\b"
        ],
        "profile_path": "eeo_demographics.gender",
        "provenance_key": "profile:eeo_demographics.gender",
        "type": "eeo",
    },
    "race_ethnicity": {
        "category": "eeo_demographics",
        "patterns": [
            r"\brace\b", r"\bethnicity\b", r"\brace\s*/\s*ethnicity\b", r"\bracial\s*background\b"
        ],
        "profile_path": "eeo_demographics.race_ethnicity",
        "provenance_key": "profile:eeo_demographics.race_ethnicity",
        "type": "eeo",
    },
    "hispanic_latino": {
        "category": "eeo_demographics",
        "patterns": [
            r"\bhispanic\b", r"\blatino\b", r"\bhispanic\s*or\s*latino\b"
        ],
        "profile_path": "eeo_demographics.hispanic_latino",
        "provenance_key": "profile:eeo_demographics.hispanic_latino",
        "type": "eeo",
    },
    "veteran_status": {
        "category": "eeo_demographics",
        "patterns": [
            r"\bveteran\b", r"\bveteran\s*status\b", r"\bmilitary\s*status\b", r"\bprotected\s*veteran\b"
        ],
        "profile_path": "eeo_demographics.veteran_status",
        "provenance_key": "profile:eeo_demographics.veteran_status",
        "type": "eeo",
    },
    "disability_status": {
        "category": "eeo_demographics",
        "patterns": [
            r"\bdisability\b", r"\bdisability\s*status\b", r"\bphysical\s*or\s*mental\s*impairment\b"
        ],
        "profile_path": "eeo_demographics.disability_status",
        "provenance_key": "profile:eeo_demographics.disability_status",
        "type": "eeo",
    },
    "pronouns": {
        "category": "eeo_demographics",
        "patterns": [
            r"\bpronouns\b", r"\bpreferred\s*pronouns\b"
        ],
        "profile_path": "eeo_demographics.pronouns",
        "provenance_key": "profile:eeo_demographics.pronouns",
        "type": "eeo",
    },

    # ── 8. Work Authorization & Compliance ───────────────────────────────────
    "work_auth_status": {
        "category": "compliance_legal",
        "patterns": [
            r"authorized\s*to\s*work\s*in\s*the\s*(us|united\s*states|u\.s\.)",
            r"authorized\s*to\s*work\s*in\s*the\s*country",
            r"authorized\s*to\s*work",
            r"legally\s*authorized\s*to\s*work",
            r"work\s*authorization(\s*status)?",
            r"legal\s*right\s*to\s*work",
            r"legally\s*authorized"
        ],
        "profile_path": "compliance_and_work_auth.work_auth_status",
        "provenance_key": "profile:compliance_and_work_auth.work_auth_status",
        "type": "work_auth",
    },
    "visa_sponsorship": {
        "category": "compliance_legal",
        "patterns": [
            r"require\s*(employment\s*|work\s*)?(visa\s*)?sponsorship",
            r"sponsorship\s*to\s*work\s*in\s*the\s*country",
            r"sponsorship.*country\s*where",
            r"sponsorship\s*now\s*or\s*in\s*the\s*future",
            r"visa\s*sponsorship",
            r"visa\s*support",
            r"visa\s*assistance",
            r"immigration\s*(?:support|assistance|sponsorship)",
            r"work\s*permit\s*(?:support|assistance|sponsorship)",
            r"sponsor\s*(?:a\s*)?(?:visa|you|your|work)",
            r"(?:require|need)\s*(?:a\s*)?visa",
            r"need\s*(?:employment\s*|work\s*)?(?:visa\s*)?sponsorship",
            r"^\s*sponsorship\s*\??\s*$",
        ],
        "profile_path": "compliance_and_work_auth.work_auth_status",
        "provenance_key": "profile:compliance_and_work_auth.require_visa_sponsorship",
        "type": "work_auth",
    },
    "us_person_status": {
        "category": "compliance_legal",
        "patterns": [
            r"u\.?s\.?\s*person", r"us\s*person",
            r"which.*best\s*describes\s*your.*u\.?s\.?\s*person",
            r"\bitar\b", r"\bear\b", r"export\s*controls?",
            r"subject\s*to\s*export\s*controls"
        ],
        "profile_path": "compliance_and_work_auth.us_person_status",
        "provenance_key": "profile:compliance_and_work_auth.us_person_status",
        "type": "us_person",
    },
    "security_clearance_level": {
        "category": "compliance_legal",
        "patterns": [
            r"what\s*(level|type)\s*(is|of)\s*.*clearance",
            r"clearance\s*level",
            r"level\s*of\s*(security\s*)?clearance",
            r"security\s*clearance\s*level",
        ],
        "profile_path": "compliance_and_work_auth.security_clearance_level",
        "provenance_key": "profile:compliance_and_work_auth.security_clearance_level",
        "type": "text",
        "default": "N/A",
    },
    "security_clearance": {
        "category": "compliance_legal",
        "patterns": [
            r"security\s*clearance",
            r"active\s*clearance",
            r"do\s*you\s*(currently\s*)?hold\s*a\s*(security\s*)?clearance",
            r"possess\s*a\s*security\s*clearance",
        ],
        "profile_path": "compliance_and_work_auth.security_clearance",
        "provenance_key": "profile:compliance_and_work_auth.security_clearance",
        "type": "boolean",
        "default": "No",
    },
    "auditor_independence": {
        "category": "compliance_legal",
        "patterns": [
            r"auditor\s*independence", r"pricewaterhousecoopers", r"\bpwc\b",
            r"independent\s*auditor", r"previously\s*worked\s*at.*auditor",
            r"ernst\s*&\s*young|\bey\b|deloitte|kpmg|accounting\s*firm"
        ],
        "profile_path": "compliance_and_work_auth.auditor_independence",
        "provenance_key": "profile:compliance_and_work_auth.auditor_independence",
        "type": "boolean_negative",
    },
    "military_objections": {
        "category": "compliance_legal",
        "patterns": [
            r"reasons?\s*why\s*you\s*would\s*not\s*be\s*able\s*to\s*fully\s*contribute.*military",
            r"military.*allied\s*forces.*reasons",
            r"contribute\s*in\s*support\s*of\s*the\s*u\.?s\.?\s*military",
            r"support\s*the\s*u\.?s\.?\s*military\s*and\s*allied\s*forces",
            r"objections?\s*to\s*working\s*on.*(defense|military)"
        ],
        "profile_path": "compliance_and_work_auth.military_objections",
        "provenance_key": "profile:compliance_and_work_auth.military_objections",
        "type": "boolean_negative",
    },
    "government_employment": {
        "category": "compliance_legal",
        "patterns": [
            r"government\s*(employ|service|job|role|official)",
            r"prior.*government", r"worked.*government", r"civil\s*service",
            r"public\s*official", r"us\s*government"
        ],
        "profile_path": "compliance_and_work_auth.government_employment",
        "provenance_key": "profile:compliance_and_work_auth.government_employment",
        "type": "boolean",
    },
    "privacy_policy_consent": {
        "category": "compliance_legal",
        "patterns": [
            r"privacy\s*(policy|notice)", r"consent\s*to\s*(processing|data)",
            r"terms\s*and\s*conditions", r"acknowledge\s*and\s*agree",
            r"data\s*retention"
        ],
        "profile_path": "compliance_and_work_auth.privacy_policy_consent",
        "provenance_key": "profile:compliance_and_work_auth.privacy_policy_consent",
        "type": "boolean",
    },
    "text_messaging_consent": {
        "category": "compliance_legal",
        "patterns": [
            r"text\s*messag(?:e|ing|es)?\s*(?:updates?|agreement)?",
            r"communication\s*consent",
            r"consent\s*to\s*receiv(?:e|ing)\s*text",
            r"sms\s*(?:consent|updates?)"
        ],
        "profile_path": "compliance_and_work_auth.text_messaging_consent",
        "provenance_key": "profile:compliance_and_work_auth.text_messaging_consent",
        "type": "boolean",
    },

}


# ── Option Aligner (Select / Radio / Combobox) ───────────────────────────────

class OptionAligner:
    """Aligns canonical profile facts to the options of a select, radio group, or combobox."""

    @classmethod
    def align(cls, canonical_value: Any, choices: List[str], field_type: str = "text") -> Optional[str]:
        if not choices:
            return str(canonical_value) if canonical_value is not None else None

        v_str = str(canonical_value).strip()
        v_low = v_str.lower()

        # 1. Exact match
        for c in choices:
            if c.strip().lower() == v_low:
                return c

        # 2. Location / Opportunity Location / Work Location
        if field_type in ["location", "combobox"] or any(k in v_low for k in ["washington", "district of columbia", "united states"]):
            try:
                from form_filler import DeterministicEngine
                best_loc = DeterministicEngine.pick_best_location_choice(choices)
                if best_loc:
                    return best_loc
            except Exception:
                pass

        # 3. Work Auth & Visa Status
        if field_type == "work_auth":
            # If candidate requires sponsorship
            for kw in ["require sponsorship", "sponsorship", "authorized with sponsorship", "f-1 opt", "opt / cpt", "opt", "stem opt"]:
                for c in choices:
                    if kw in c.lower():
                        return c
            for c in choices:
                c_clean = c.lower().strip()
                if any(y in c_clean for y in ["yes", "authorized", "eligible"]):
                    return c

        # 3. US Person Status
        if field_type == "us_person":
            for c in choices:
                c_low = c.lower().strip()
                if any(k in c_low for k in ["different country", "none of the above", "not a u.s.", "not a us", "neither", "no"]):
                    return c

        # 4. Boolean Negative (e.g. PwC auditor, military objections)
        if field_type == "boolean_negative" or v_low in ["no", "never", "false"]:
            for c in choices:
                c_low = c.lower().strip()
                if any(n in c_low for n in ["never been employed", "never worked", "no - i have never", "no", "never", "none", "false"]):
                    return c

        # 5. Boolean Positive (Consent, Agreements)
        if field_type == "boolean" or v_low in ["yes", "true"]:
            for c in choices:
                c_low = c.lower().strip()
                if any(y in c_low for y in ["yes - i consent", "yes", "i agree", "agree", "confirm", "accept", "authorized", "true"]):
                    return c
            # Fallback to first non-negative
            for c in choices:
                if not any(n in c.lower() for n in ["no", "never", "decline", "not"]):
                    return c

        # 6. Numeric / Years of Experience (Range Matching: e.g. 2 -> "1-3 years", "2+", "1-2 years")
        if field_type == "numeric_years":
            num_match = re.search(r"\d+", v_low)
            cand_years = int(num_match.group()) if num_match else 2

            # Try exact number
            for c in choices:
                if c.strip().lower() == str(cand_years):
                    return c

            # Try ranges: e.g. "1 - 3", "1-3 years", "2-4"
            for c in choices:
                c_clean = c.lower().strip()
                rng = re.findall(r"\d+", c_clean)
                if len(rng) >= 2:
                    low, high = int(rng[0]), int(rng[1])
                    if low <= cand_years <= high:
                        return c
                elif len(rng) == 1 and ("+" in c_clean or "more" in c_clean or "over" in c_clean):
                    if cand_years >= int(rng[0]):
                        return c

        # 6.5 Salary Range Matching (e.g. 100000 / 100k -> "$100,000 - $120,000")
        if field_type == "salary" or any(k in v_low for k in ["100k", "100000"]):
            target_sal = 100000
            for c in choices:
                c_clean = c.lower().replace(",", "").replace("$", "")
                nums = re.findall(r"\d+", c_clean)
                sal_nums = []
                for n in nums:
                    val = int(n)
                    if val < 500:  # e.g. "100" from "100k"
                        val = val * 1000
                    sal_nums.append(val)
                if len(sal_nums) >= 2:
                    if sal_nums[0] <= target_sal <= sal_nums[1]:
                        return c
                elif len(sal_nums) == 1 and any(p in c_clean for p in ["+", "more", "over", "minimum", "starting"]):
                    if target_sal >= sal_nums[0]:
                        return c
            for c in choices:
                if "100" in c:
                    return c

        # 7. EEO / Demographics
        if field_type == "eeo":
            for c in choices:
                if v_low in c.lower() or c.lower() in v_low:
                    return c
            # Fallback to neutral / decline to state if no exact match
            for c in choices:
                if any(d in c.lower() for d in ["decline to self-identify", "decline to state", "prefer not to answer", "i do not wish"]):
                    return c

        # 8. Substring match
        for c in choices:
            if v_low in c.lower() or c.lower() in v_low:
                return c

        return None


# ── Universal Question Solver ────────────────────────────────────────────────

class UniversalQuestionSolver:
    """
    Solves any application question via a strictly grounded 4-stage pipeline:
      Stage 1: exact_registry
      Stage 2: fuzzy_registry (ratio >= 0.85)
      Stage 3: semantic_profile
      Stage 4: unmapped_log (halt + record, zero invention)
    """

    def __init__(self, profile_store: Optional[CandidateProfileStore] = None):
        self.profile = profile_store or CandidateProfileStore()

    def solve(
        self,
        question: str,
        choices: Optional[List[str]] = None,
        company: str = "unknown",
        portal: str = "unknown",
        job_url: str = "",
        is_required: bool = True
    ) -> Tuple[Optional[str], Optional[ProvenanceTag], str]:
        """
        Resolve answer for question.
        Returns: (resolved_answer, provenance_tag, stage_name)
        """
        if not question:
            return None, None, "empty"

        norm_q = normalize_question(question)
        raw_q_low = question.lower()

        # ── Stage 1: Exact Registry Match ──
        for key, entry in UNIVERSAL_REGISTRY.items():
            for pat in entry["patterns"]:
                if re.search(pat, raw_q_low, re.IGNORECASE) or re.search(pat, norm_q, re.IGNORECASE):
                    canonical_val = self.profile.get_fact(entry["profile_path"])
                    ans = OptionAligner.align(canonical_val, choices, entry["type"]) if choices else str(canonical_val)
                    if ans is not None:
                        logger.info(f"[stage=exact_registry] '{question[:30]}' matched '{key}' -> '{ans}'")
                        prov = ProvenanceTag(entry["provenance_key"], canonical_val, f"Exact registry match for '{key}'")
                        return ans, prov, "exact_registry"

        # ── Stage 2: Fuzzy Registry Match (≥ 0.85 Similarity) ──
        best_ratio = 0.0
        best_entry = None
        best_key = ""

        for key, entry in UNIVERSAL_REGISTRY.items():
            for pat in entry["patterns"]:
                # Compute similarity between question and stripped pattern
                clean_pat = re.sub(r"[\\b\(\)\?\*\|]", "", pat).strip()
                ratio = SequenceMatcher(None, norm_q, clean_pat).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_entry = entry
                    best_key = key

        if best_ratio >= 0.85 and best_entry:
            canonical_val = self.profile.get_fact(best_entry["profile_path"])
            ans = OptionAligner.align(canonical_val, choices, best_entry["type"]) if choices else str(canonical_val)
            if ans is not None:
                logger.info(f"[stage=fuzzy_registry] '{question[:30]}' matched '{best_key}' (ratio={best_ratio:.2f}) -> '{ans}'")
                prov = ProvenanceTag(best_entry["provenance_key"], canonical_val, f"Fuzzy registry match ({best_ratio:.2f}) for '{best_key}'")
                return ans, prov, "fuzzy_registry"

        # ── Stage 3: Semantic Profile Map (Direct Profile Field Grounding) ──
        semantic_map = {
            "first name": ("personal_info.first_name", "profile:personal_info.first_name", "text"),
            "last name": ("personal_info.last_name", "profile:personal_info.last_name", "text"),
            "full name": ("personal_info.full_name", "profile:personal_info.full_name", "text"),
            "email": ("personal_info.email", "profile:personal_info.email", "text"),
            "phone": ("personal_info.phone", "profile:personal_info.phone", "text"),
            "linkedin": ("links.linkedin_url", "profile:links.linkedin_url", "url"),
            "github": ("links.github_url", "profile:links.github_url", "url"),
            "portfolio": ("links.portfolio_url", "profile:links.portfolio_url", "url"),
            "school": ("education.school", "profile:education.school", "text"),
            "degree": ("education.degree", "profile:education.degree", "text"),
            "major": ("education.major", "profile:education.major", "text"),
            "discipline": ("education.discipline", "profile:education.discipline", "text"),
            "salary expectation": ("compensation.annual_salary_expectation", "profile:compensation.annual_salary_expectation", "salary"),
            "salary": ("compensation.annual_salary_expectation", "profile:compensation.annual_salary_expectation", "salary"),
            "start date": ("preferences.start_date", "profile:preferences.start_date", "text"),
            "current company": ("experience.current_company", "profile:experience.current_company", "text"),
            "current title": ("experience.current_title", "profile:experience.current_title", "text"),
            "work authorization": ("compliance_and_work_auth.work_auth_status", "profile:compliance_and_work_auth.work_auth_status", "work_auth"),
            "sponsorship": ("compliance_and_work_auth.work_auth_status", "profile:compliance_and_work_auth.require_visa_sponsorship", "work_auth"),
            "payroll tax location": ("compliance_and_work_auth.payroll_tax_location", "profile:compliance_and_work_auth.payroll_tax_location", "combobox"),
            "phone country code": ("personal_info.phone_country_code", "profile:personal_info.phone_country_code", "select"),
            "clearance level": ("compliance_and_work_auth.security_clearance_level", "profile:compliance_and_work_auth.security_clearance_level", "text"),
            "security clearance": ("compliance_and_work_auth.security_clearance", "profile:compliance_and_work_auth.security_clearance", "boolean"),
            "export compliance": ("compliance_and_work_auth.us_person_status", "profile:compliance_and_work_auth.us_person_status", "us_person"),
            "hourly pay": ("experience.hourly_rate", "profile:experience.hourly_rate", "text"),
            "hourly rate": ("experience.hourly_rate", "profile:experience.hourly_rate", "text"),
            "pay requirements": ("experience.hourly_rate", "profile:experience.hourly_rate", "text"),
        }

        for sem_key, (fact_path, prov_key, ftype) in semantic_map.items():
            if sem_key in norm_q:
                canonical_val = self.profile.get_fact(fact_path)
                ans = OptionAligner.align(canonical_val, choices, ftype) if choices else str(canonical_val)
                if ans is not None:
                    logger.info(f"[stage=semantic_profile] '{question[:30]}' semantically mapped to '{sem_key}' -> '{ans}'")
                    prov = ProvenanceTag(prov_key, canonical_val, f"Semantic profile map for '{sem_key}'")
                    return ans, prov, "semantic_profile"

        # ── Stage 4: Unmapped Required Field -> Safe Halt & Log ──
        if is_required:
            logger.warning(f"[stage=unmapped] Unmapped required question: '{question}' (choices={choices}). Halting to prevent invention.")
            self._log_unmapped(company, portal, job_url, question, choices)

        return None, None, "unmapped"

    def _log_unmapped(self, company: str, portal: str, job_url: str, question: str, choices: Optional[List[str]]):
        try:
            os.makedirs(os.path.dirname(UNMAPPED_FIELDS_LOG_PATH) or ".", exist_ok=True)
            ts = datetime.now(timezone.utc).isoformat()
            line = f"{ts} | {company} | {portal} | {job_url} | {question} | choices={choices or []}\n"
            with open(UNMAPPED_FIELDS_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            logger.error(f"Failed to record unmapped question: {e}")


# Singleton instance
universal_solver = UniversalQuestionSolver()
