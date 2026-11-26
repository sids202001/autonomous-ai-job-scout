"""
Form Guards & Decision Engine (Audit Rules 2-10, 13, 15)

Strict, deterministic policy layer enforcing:
- Single source of truth (profile.json)
- Clear-before-fill & read-back verification (Rule 2)
- Dismiss cookie banners least-permissive first (Rule 3)
- Relocation / In-office / Hub safe-halt: RELOCATION_DECISION_REQUIRED (Rule 4)
- "Which Visa?" = H-1B or VISA_OPTION_MISSING (Rule 5)
- Essays / Custom open-text / Self-ratings safe-halt: ESSAY_HALT (Rule 6)
- Contradictory-premise questions safe-halt: PREMISE_MISMATCH (Rule 7)
- Home address safe-halt: ADDRESS_REQUIRED (Rule 8)
- How did you hear: Company website first, never LinkedIn: SOURCE_OPTION_MISSING (Rule 9)
- Fine-grained communications consent: CONSENT_AMBIGUOUS (Rule 10)
"""

import re
import os
import logging
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Halt Classes & Exceptions
# ============================================================================

class FormHaltException(Exception):
    """Base exception for all intentional application safe-halts."""
    def __init__(
        self,
        code: str,
        message: str,
        question: str = "",
        choices: Optional[List[str]] = None,
        screenshot_path: Optional[str] = None
    ):
        self.code = code
        self.reason = code
        self.message = message
        self.question = question
        self.choices = choices or []
        self.screenshot_path = screenshot_path
        super().__init__(f"[{code}] {message}")


class FillVerificationFailedError(FormHaltException):
    """Raised when clear-before-fill or selection read-back fails after retry."""
    def __init__(self, field_name: str, intended: Any, actual: Any, screenshot_path: Optional[str] = None):
        msg = f"Field verification failed for '{field_name}': intended='{intended}', actual='{actual}'"
        super().__init__("FILL_VERIFICATION_FAILED", msg, question=field_name, screenshot_path=screenshot_path)
        self.field_name = field_name
        self.intended = intended
        self.actual = actual


# ============================================================================
# Rule 4 & Addendum (Nov 18): Relocation & Proximity Matchers & Solvers
# ============================================================================

DC_METRO_KEYWORDS = [
    "washington, dc", "washington dc", "washington", "district of columbia",
    "arlington", "alexandria", "reston", "tysons", "mclean", "bethesda",
    "silver spring", "northern virginia", "nova"
]

CANONICAL_RELOCATION_WILLING = "Yes — anywhere in the United States"

# Relocation Assistance keywords (always safe-halt: RELOCATION_ASSISTANCE_HALT)
RELOCATION_ASSISTANCE_PATTERNS = [
    r"(?:relocation|moving)\s*(?:assistance|package|expenses|stipend|reimbursement|bonus|cost|support)",
    r"(?:require|request|need)\s*(?:any\s*)?(?:relocation|moving)\s*assistance",
    r"financial\s*assistance\s*to\s*relocate",
    r"will\s*you\s*(?:require|need)\s*relocation\s*assistance",
    r"do\s*you\s*(?:require|need)\s*relocation\s*assistance",
]

# Relocation Timing keywords (always safe-halt: RELOCATION_TIMING_HALT)
RELOCATION_TIMING_PATTERNS = [
    r"(?:when|how\s*soon)\s*(?:can|could|would)\s*you\s*(?:be\s*able\s*to\s*)?relocate",
    r"relocation\s*timeline",
    r"date\s*by\s*which\s*you\s*(?:can|could)\s*relocate",
]

# Remote-only preference/willingness keywords (always safe-halt: REMOTE_WILLINGNESS_HALT)
REMOTE_WILLINGNESS_PATTERNS = [
    r"willing\s*to\s*work\s*(?:fully|100%)\s*remote",
    r"prefer\s*(?:fully\s*)?remote",
    r"remote\s*work\s*preference",
    r"only\s*interested\s*in\s*remote",
]

# Current location / proximity keywords (answered strictly from profile DC location)
PROXIMITY_50_MILES_PATTERNS = [
    r"within\s*\d+\s*miles",
    r"within\s*50\s*miles",
    r"located\s*within\s*(?:a\s*)?commuting\s*distance",
    r"are\s*you\s*(?:currently\s*)?located\s*within",
    r"do\s*you\s*(?:currently\s*)?live\s*within",
    r"live\s*within\s*\d+\s*miles",
    r"which\s*office\s*are\s*you\s*(?:closest\s*to|near)",
    r"which\s*(?:of\s*our\s*)?(?:office\s*)?hubs?\s*are\s*you\s*(?:located\s*within|near)",
    r"which\s*location\s*are\s*you\s*(?:located\s*in|closest\s*to)",
    r"commutable\s*distance\s*to\s+[A-Za-z]+",
    r"commuting\s*distance\s*to\s+[A-Za-z]+",
]

# Hub selection keywords when choices represent distinct physical office cities
HUB_LOCATION_KEYWORDS = [
    r"which\s*location\s*are\s*you\s*applying\s*for",
    r"opportunity\s*location",
    r"opportunitylocationid",
    r"preferred\s*office",
    r"office\s*location",
    r"select\s*(?:an?|your)?\s*location",
    r"workplace\s*location",
    r"hub\s*location",
    r"location\s*preference",
]

# Relocation Willingness keywords (answered "Yes" via canonical profile)
RELOCATION_WILLINGNESS_PATTERNS = [
    r"willing\s*to\s*relocate",
    r"open\s*to\s*relocat",
    r"would\s*you\s*relocate",
    r"are\s*you\s*(?:local\s*to\s*or\s*)?willing\s*to\s*relocate",
    r"local\s*to\s*or\s*willing\s*to\s*relocate",
    r"ability\s*to\s*relocate",
    r"willingness\s*to\s*relocate",
    r"open\s*to\s*relocation",
    r"relocat(?:e|ion|ing)\s*(?:for|to|before)",
    r"(?:willing|open|able|comfortable)\s*to\s*(?:work(?:ing)?|be(?:ing)?)\s*(?:in-?person|in-?office|on-?site|hybrid)",
    r"in-?office\s*\d+x\s*(?:per\s*week|a\s*week)?",
    r"willing\s*to\s*work\s*in-?person",
    r"willing\s*to\s*work\s*in-?office",
    r"willing\s*to\s*work\s*on-?site",
    r"willing\s*to\s*work\s*in\s*(?:our|a|an)",
    r"able\s*to\s*work\s*in-?person",
    r"able\s*to\s*work\s*in-?office",
    r"able\s*to\s*work\s*on-?site",
    r"work\s*(?:from|in)\s*(?:an?|our|the)?\s*office",
    r"office\s*presence",
    r"days?\s*(?:per|\/|a)\s*week",
    r"(?:three|four|five|2|3|4|5)\s*days\s*(?:a|per)\s*week",
    r"hybrid\s*(?:schedule|role|work|environment|model|policy)",
    r"onsite\s*(?:schedule|role|work|environment|model|policy|requirement)",
    r"on-site\s*(?:schedule|role|work|environment|model|policy|requirement)",
    r"in-?person\s*(?:role|work|requirement|collaboration|days|culture)",
    r"office\s*culture",
    r"in-?person\s*culture",
    r"in-?office\s*policy",
    r"onsite\s*culture",
    r"collaborat(?:e|ing)\s*in-?person",
    r"in-?office\s*collaboration",
    r"culture\s*acknowledgement",
    r"willing\s*to\s*commute",
    r"open\s*to\s*commuting",
]

# Exclusion: Questions asking purely for candidate's residential address / country
RESIDENTIAL_RESIDENCE_PATTERNS = [
    r"country\s*of\s*residence",
    r"current\s*country",
    r"where\s*do\s*you\s*(?:currently\s*)?reside",
    r"city\s*and\s*state\s*(?:you\s*)?reside",
    r"are\s*you\s*located\s*in\s*the\s*(?:united\s*states|us|usa)",
    r"do\s*you\s*currently\s*reside\s*in\s*the\s*u\.?s\.?",
    r"current\s*address",
    r"current\s*city",
    r"current\s*location",
]


def is_relocation_assistance_question(question: str) -> bool:
    """
    Updated Nov 22: Relocation assistance now has canonical answers in profile.json
    ('Do you require relocation assistance?' -> No; 'Are you open to relocation assistance if provided?' -> Yes).
    Returns False so the bot answers canonically instead of halting.
    """
    return False


TRAVEL_DECISION_PATTERNS = [
    r"(?:willing|able|open|comfortable)\s*(?:to\s*)?travel",
    r"travel\s*(?:requirement|percentage|up\s*to|\d+\s*(?:%|percent|to|-))",
    r"\d+\s*(?:to|-)\s*\d+\s*(?:%|percent)\s*travel",
    r"involves?\s*\d+.*travel",
    r"multi-?week\s*(?:stretches?\s*)?on-?\s*site",
]


def is_travel_decision_question(question: str) -> bool:
    """R9 (updated Nov 22): Travel 30-50% is now user-confirmed Yes — do not halt on travel willingness."""
    return False


def solve_canonical_screening_question(
    question: str,
    choices: Optional[List[str]] = None,
    is_required: bool = True,
) -> Optional[str]:
    """
    R9 canonical screening answers from Siddhesh's record (profile.json, updated Nov 22):
    - Remote work willingness -> Yes
    - Relocation assistance: 'Do you require relocation assistance?' -> No (can relocate at own expense);
      'Are you open to relocation assistance if the company provides it?' -> Yes
    - Applied to same position in last 3 months -> No
    - >3 years deploying solutions at customers -> No
    - Met at a career fair -> No
    - Currently a Twitch / company employee / non-compete -> No
    - Previously employed by Amazon / subsidiary -> Yes; Current employee with Amazon -> No; Previously applied to Amazon -> Yes
    - Travel 30-50% (multi-week on-site) -> Yes (user-confirmed Nov 22)
    - May we contact your current employer? -> Yes (user-confirmed Nov 22)
    - Sexual orientation -> Heterosexual (Straight) (never Asexual!)
    - Transgender -> No
    - Work authorization expiration date -> 07/08/2027
    - Bachelor's completion -> Aug 2022 (or 2022 in year dropdown)
    - Live in NY/CA -> No; Options 101 -> No; Prior options market-making -> No; Currently pursuing degree -> No
    - HRIS/ATS extraction or GCP production work -> 'None of the above' if offered, else HALT
    - GCP experience text question -> HALT (no GCP on record)
    - Embedded with enterprise customer -> HALT (unconfirmed on record)
    - Travel / travel-adjacent industry experience -> HALT (no travel industry experience on record)
    """
    if not question:
        return None
    q = question.strip()
    q_low = q.lower()

    def _pick_no() -> str:
        if choices:
            for c in choices:
                c_s = str(c).strip()
                if re.match(r"^(?:no|false)\b", c_s, re.IGNORECASE) and not re.search(r"\byes\b", c_s, re.IGNORECASE):
                    return c_s
            for c in choices:
                c_s = str(c).strip()
                if re.search(r"own\s+expense|without\s+assistance|do\s+not\s+require", c_s, re.IGNORECASE):
                    return c_s
            # General rule: for completion/progress questions, "Not yet!" / "Not yet" / "Have not" maps to No
            for c in choices:
                c_s = str(c).strip()
                if re.match(r"^(?:not\s+yet|haven'?t|i\s+haven'?t|have\s+not|i\s+have\s+not|not\s+started)\b", c_s, re.IGNORECASE):
                    return c_s
            raise FormHaltException(
                "UNMAPPED_SCREENING_QUESTION",
                f"Canonical answer is 'No' for '{q}', but no 'No' option was found among {choices}.",
                question=q,
                choices=choices,
            )
        return "No"

    def _pick_yes() -> str:
        if choices:
            for c in choices:
                c_s = str(c).strip()
                if re.match(r"^(?:yes|true)\b", c_s, re.IGNORECASE) and not re.search(r"\bno\b", c_s, re.IGNORECASE):
                    return c_s
            raise FormHaltException(
                "UNMAPPED_SCREENING_QUESTION",
                f"Canonical answer is 'Yes' for '{q}', but no 'Yes' option was found among {choices}.",
                question=q,
                choices=choices,
            )
        return "Yes"

    # 00a. Remote work willingness -> Yes (user-confirmed Nov 22)
    if any(re.search(pat, q_low, re.IGNORECASE) for pat in REMOTE_WILLINGNESS_PATTERNS) or re.search(r"(?:willing|open|able|comfortable)\s+to\s+work\s+remotely", q_low):
        return _pick_yes()

    # 00b. Relocation assistance (user-confirmed Nov 22):
    # - "Are you open to relocation assistance if the company provides it?" -> Yes
    # - "Do you require relocation assistance?" -> No (can relocate at own expense)
    if not (re.search(r"offers?\s+relocation\s+assistance", q_low) and re.search(r"(?:open\s+to\s+working|willing\s+to\s+(?:work|relocate)|in-?office|on-?site|hybrid)", q_low)):
        if re.search(r"(?:are\s+you\s+open|would\s+you\s+be\s+open|interested\s+in)\s+(?:to\s+)?(?:receiving\s+)?(?:relocation|moving)\s+assistance|relocation\s+assistance\s+if\s+(?:the\s+company\s+)?(?:provides|offers)", q_low):
            return _pick_yes()
        if any(re.search(pat, q_low, re.IGNORECASE) for pat in RELOCATION_ASSISTANCE_PATTERNS):
            return _pick_no()

    # 0a. Amazon employment / application history (AWS Mar 2025 - Mar 2026)
    if re.search(r"current\s+employee\s+with\s+amazon|currently\s+employed\s+(?:by|at|with)\s+amazon", q_low):
        return _pick_no()
    if re.search(r"previously\s+(?:been\s+)?employed\s+(?:by|at|with)\s+amazon|previously\s+applied\s+to\s+amazon", q_low):
        return _pick_yes()

    # 0b. May we contact your current employer? -> Yes (user-confirmed Nov 22)
    if re.search(r"may\s+we\s+contact\s+your\s+current\s+employer|contact\s+your\s+current\s+employer", q_low):
        return _pick_yes()

    # 0c. Travel 30-50% / travel willingness -> Yes (user-confirmed Nov 22)
    if not re.search(r"experience\s+in\s+travel", q_low) and any(re.search(pat, q_low, re.IGNORECASE) for pat in TRAVEL_DECISION_PATTERNS):
        return _pick_yes()

    # 0d. Sexual orientation -> Heterosexual / Straight (NEVER Asexual!)
    if re.search(r"sexual\s*orientation", q_low):
        if choices:
            for c in choices:
                c_s = str(c).strip()
                if re.search(r"heterosexual|straight", c_s, re.IGNORECASE):
                    return c_s
        return "Heterosexual"

    # 0e. Transgender -> No; Cisgender/Transgender "I identify as:" -> Cisgender
    if choices and any(re.search(r"\bcisgender\b", str(c), re.IGNORECASE) for c in choices):
        for c in choices:
            c_s = str(c).strip()
            if re.search(r"\bcisgender\b", c_s, re.IGNORECASE) and not re.search(r"\b(?:non-?cisgender|female|woman)\b", c_s, re.IGNORECASE):
                return c_s
    if re.search(r"\btransgender\b", q_low):
        return _pick_no()

    # 0e2. Ethnicity / Race (including plural "ethnicities") when choices are offered -> South Asian if offered, else Asian
    # (When choices is None, defer to form_filler.DeterministicEngine so profile.json compliance_and_eeo.race_ethnicity is read dynamically.)
    if choices and re.search(r"\b(race|racial|ethnic|ethnicit(?:y|ies))\b", q_low) and not re.search(r"race\s*condition", q_low):
        for target in ("south asian", "asian (not hispanic or latino)", "asian"):
            for c in choices:
                c_s = str(c).strip()
                c_low = c_s.lower()
                if "caucasian" in c_low:
                    continue
                if target == "asian" and re.search(r"\b(?:east|southeast|west)\s+asian\b", c_low):
                    continue
                if target in c_low:
                    return c_s
        for c in choices:
            c_s = str(c).strip()
            if "asian" in c_s.lower() and "caucasian" not in c_s.lower():
                return c_s

    # 0e3. Legal name confirmation ("Have you added your full legal name and surname (including any middle names)?") -> Yes
    if re.search(
        r"(?:have\s+you\s+(?:added|entered|included|provided|used)|did\s+you\s+(?:add|enter|include|provide|use)).*"
        r"(?:full\s+legal\s+name|legal\s+name|surname|middle\s+name)",
        q_low,
    ):
        return _pick_yes()

    # 0f. Work authorization expiration date -> 07/08/2027
    if re.search(r"work\s*authorization.*expir|authorization\s*expir|opt.*expir", q_low):
        return "07/08/2027"

    # 0g. Bachelor's completion year / date -> Aug 2022 (or 2022)
    if re.search(r"bachelor'?s?.*(?:complet|graduat)|(?:complet|graduat).*bachelor'?s?", q_low):
        if choices:
            for c in choices:
                if "2022" in str(c):
                    return str(c).strip()
        return "Aug 2022"

    # 0h. Live in NY/CA, Options 101, Prior options market-making -> No
    if re.search(r"live\s+in\s+(?:new\s+york|ny|california|ca)|options\s*101|options\s*market[- ]making", q_low):
        return _pick_no()

    # 1. Applied to same position in last N months -> No
    if re.search(r"applied\s+to\s+(?:that|this|the)\s+same\s+(?:position|role|job)|applied.*in\s+the\s+last\s+\d+\s+months", q_low):
        return _pick_no()

    # 2. >3 years deploying solutions at customers -> No
    if re.search(r"more\s+than\s+(?:three|3)\s+years?\s+deploying|3\+\s*years?\s+deploying", q_low):
        return _pick_no()

    # 3. Did you meet us at a career fair? -> No
    if re.search(r"(?:did\s+you\s+)?me(?:et|t)\s+(?:us|someone|our\s+team)\s+at\s+a\s+career\s+fair|career\s+fair", q_low):
        return _pick_no()

    # 4. Currently an employee of [Company] / non-compete -> No
    if re.search(r"are\s+you\s+currently\s+(?:a|an)\s+[\w\s]{1,30}\s+employee|non-?compet(?:e|ition)\s+agreement", q_low):
        return _pick_no()

    # 4b. Currently based / located in the United States, or legal rights to work in the US -> Yes
    if re.search(r"(?:currently\s+)?(?:based|located|residing|living)\s+in\s+the\s+(?:united\s+states|u\.?s\.?a?\.?)\b", q_low):
        return _pick_yes()
    if re.search(r"legal\s+rights?\s+to\s+work\s+in\s+(?:the\s+)?(?:united\s+states|u\.?s\.?a?\.?)\b", q_low):
        return _pick_yes()

    # 5. HRIS/ATS extraction or GCP production checkboxes -> 'None of the above' if offered, else HALT
    if re.search(r"(?:hris|ats\s*data|workday.*greenhouse|gcp\s*(?:production|experience|tools|bigquery))", q_low):
        if choices:
            for c in choices:
                c_s = str(c).strip()
                if re.search(r"none\s+of\s+the\s+above|^none$|not\s+applicable", c_s, re.IGNORECASE):
                    return c_s
        if is_required:
            raise FormHaltException(
                "UNMAPPED_SCREENING_QUESTION",
                f"Candidate has no GCP / HRIS-ATS extraction experience on record for '{q}'. Available options: {choices or []}. Halting without inventing.",
                question=q,
                choices=choices,
            )
        return ""

    # 6. Embedded with enterprise customer -> HALT (unconfirmed on record)
    if re.search(r"embedded\s+with\s+(?:an?\s+)?enterprise\s+customer", q_low):
        if is_required:
            raise FormHaltException(
                "UNMAPPED_SCREENING_QUESTION",
                f"No confirmed record for embedded enterprise customer deployment ('{q}'). Options: {choices or []}. Halting to ask user.",
                question=q,
                choices=choices,
            )
        return ""

    # 7. Travel / travel-adjacent industry experience -> HALT (never invent travel experience)
    if re.search(r"experience\s+in\s+travel|travel-?adjacent\s+services", q_low):
        if is_required:
            raise FormHaltException(
                "UNMAPPED_SCREENING_QUESTION",
                f"Candidate has no travel-industry experience on record for '{q}'. Options: {choices or []}. Halting without guessing.",
                question=q,
                choices=choices,
            )
        return ""

    return None


def is_relocation_timing_question(question: str) -> bool:
    """Returns True if the question asks when or how soon the candidate can relocate."""
    if not question:
        return False
    return any(re.search(pat, question.strip(), re.IGNORECASE) for pat in RELOCATION_TIMING_PATTERNS)


def is_remote_willingness_question(question: str) -> bool:
    """Updated Nov 22: Remote work willingness is now canonically 'Yes' in profile.json — do not halt."""
    return False


def is_current_location_or_proximity_question(question: str, choices: Optional[List[str]] = None) -> bool:
    """
    Returns True for questions about where candidate currently lives or proximity/commute to hubs.
    Narrowed per Addendum (Nov 18): Willingness questions are excluded.
    """
    if not question:
        return False
    q = question.strip()

    # Never treat relocation assistance or timing as proximity
    if is_relocation_assistance_question(q) or is_relocation_timing_question(q):
        return False

    # If it is a willingness question, it is NOT current-location proximity
    if re.search(r"\b(?:willing\s*to\s*relocate|open\s*to\s*relocat|would\s*you\s*relocate|local\s*to\s*or\s*willing)\b", q, re.IGNORECASE):
        return False

    # Check proximity / 50-miles patterns
    if any(re.search(pat, q, re.IGNORECASE) for pat in PROXIMITY_50_MILES_PATTERNS):
        return True

    # Check hub/office selection where choices are physical cities/hubs
    if choices and len(choices) > 1:
        if any(re.search(pat, q, re.IGNORECASE) for pat in HUB_LOCATION_KEYWORDS):
            hub_cities = ["san francisco", "sf", "union square", "new york", "nyc", "seattle", "austin", "boston", "chicago", "los angeles", "denver", "phoenix", "pittsburgh"]
            if any(any(h in c.lower() for h in hub_cities) for c in choices):
                return True
            if not any(re.search(r"^(yes|no)$", c.strip(), re.IGNORECASE) for c in choices):
                return True

    return False


def is_relocation_willingness_question(question: str, choices: Optional[List[str]] = None) -> bool:
    """
    Returns True for relocation willingness, in-office willingness, hybrid schedule willingness,
    and 'local to or willing to relocate' prongs. Answered Yes from profile.json.
    """
    if not question:
        return False
    q = question.strip()

    # Exclude assistance, timing, and remote preference (which halt)
    if is_relocation_assistance_question(q) or is_relocation_timing_question(q) or is_remote_willingness_question(q):
        return False

    # Exclude current-location / proximity questions (which are answered from DC profile or halt)
    if is_current_location_or_proximity_question(q, choices):
        return False

    # Pure residential check
    if any(re.search(pat, q, re.IGNORECASE) for pat in RESIDENTIAL_RESIDENCE_PATTERNS):
        return False

    return any(re.search(pat, q, re.IGNORECASE) for pat in RELOCATION_WILLINGNESS_PATTERNS)


def solve_relocation_willingness(question: str, choices: Optional[List[str]] = None) -> str:
    """
    Answers relocation willingness questions using the canonical user-stated answer (Nov 18):
    'Yes — anywhere in the United States'.
    Verifies affirmative option selection.
    """
    canonical_val = CANONICAL_RELOCATION_WILLING
    try:
        from app.services.profile_verifier import load_canonical_profile
        prof = load_canonical_profile()
        canonical_val = (
            prof.get("relocation_willing")
            or prof.get("personal_info", {}).get("relocation_willing")
            or prof.get("preferences", {}).get("relocation_willing")
            or CANONICAL_RELOCATION_WILLING
        )
    except Exception:
        pass

    if not choices:
        return canonical_val

    # 1. Exact canonical substring match
    for c in choices:
        c_str = str(c).strip()
        if re.search(r"anywhere\s*in\s*(?:the\s*)?(?:united\s*states|u\.?s\.?)", c_str, re.IGNORECASE):
            return c_str

    # 2. Relocation-specific affirmative choices
    for c in choices:
        c_str = str(c).strip()
        if re.search(r"(?:willing\s*to\s*relocate|open\s*to\s*relocat|local\s*to\s*or\s*willing|relocat)", c_str, re.IGNORECASE) and not re.search(r"(?:not\s*willing|unwilling|cannot|neither|no)", c_str, re.IGNORECASE):
            return c_str

    # 3. Standard Yes / Agree / Acknowledge choices
    for c in choices:
        c_str = str(c).strip()
        if re.match(r"^(?:yes|true|i\s*agree|agree|yes,?\s*i\s*(?:am|do|can|agree)|i\s*acknowledge|acknowledge)$", c_str, re.IGNORECASE):
            return c_str

    # 4. Any choice starting with Yes
    for c in choices:
        c_str = str(c).strip()
        if re.match(r"^yes\b", c_str, re.IGNORECASE):
            return c_str

    # If no affirmative choice matches, halt with missing option
    raise FormHaltException(
        "RELOCATION_OPTION_MISSING",
        f"Relocation willingness question '{question}' does not contain an affirmative choice among {choices}",
        question=question,
        choices=choices
    )


def solve_proximity_or_current_location(question: str, choices: Optional[List[str]] = None, is_required: bool = True) -> Optional[str]:
    """
    Evaluates current location / proximity / commute / 50-miles questions strictly
    against candidate profile location (Washington, District of Columbia, United States).
    - If hub is in DC metro: answers Yes / selects DC hub.
    - If hub is NOT in DC metro: answers No / selects truthful negative or Remote option.
    - If no truthful option exists among choices: HALTS with HUB_NOT_NEAR_RESIDENCE or PROXIMITY_REQUIREMENT_UNMET.
      NEVER selects a hub candidate does not live near (Writer SF Union Square bug prevented).
    """
    q_norm = question.strip().lower()

    # Check if question mentions a specific DC metro hub
    is_dc_hub_in_question = any(dc in q_norm for dc in DC_METRO_KEYWORDS)

    if choices:
        # Check if any choice is a DC metro location (excluding Washington State / Seattle)
        for c in choices:
            c_str = str(c).strip()
            c_low = c_str.lower()
            if "seattle" in c_low or "washington state" in c_low or "bellevue" in c_low or "redmond" in c_low:
                continue
            if any(dc in c_low for dc in DC_METRO_KEYWORDS):
                return c_str

        truthful_negative_patterns = [
            r"no,?\s*(?:but\s*)?(?:i\s*am\s*)?willing\s*to\s*relocate",
            r"no,?\s*open\s*to\s*relocat",
            r"^(?:no|false)$",
            r"none\s*of\s*the\s*above",
            r"^none$",
            r"outside\s*(?:of\s*)?\d+\s*miles",
            r"not\s*(?:located\s*)?within\s*\d+\s*miles",
            r"neither",
            r"not\s*applicable",
            r"^n\/?a$",
            r"remote\s*(?:-\s*us|\(us\)|us)?$",
            r"us\s*remote",
        ]

        if is_dc_hub_in_question:
            # Hub is DC metro, look for affirmative Yes/True
            for c in choices:
                c_str = str(c).strip()
                if re.match(r"^(?:yes|true)$", c_str, re.IGNORECASE):
                    return c_str
        else:
            # Hub is NOT in DC metro: look for truthful negative / remote
            for pat in truthful_negative_patterns:
                for c in choices:
                    c_str = str(c).strip()
                    if re.search(pat, c_str, re.IGNORECASE):
                        return c_str

        # If we reach here, choices contain only non-DC hubs (e.g. Writer SF Union Square)
        code = "HUB_NOT_NEAR_RESIDENCE" if choices and not any(re.match(r"^(yes|no)$", c.strip(), re.IGNORECASE) for c in choices) else "PROXIMITY_REQUIREMENT_UNMET"
        raise FormHaltException(
            code,
            f"Proximity requirement cannot be truth-verified: candidate lives in Washington, DC. Available choices {choices} offer no DC metro hub or truthful negative/remote option.",
            question=question,
            choices=choices
        )
    else:
        # Free text response
        return "Yes" if is_dc_hub_in_question else "No"


def is_relocation_or_in_office_question(question: str, choices: Optional[List[str]] = None) -> bool:
    """
    Deprecated Rule 4 umbrella detector.
    Returns True ONLY for questions that require a halt / human decision (assistance, timing, remote preference).
    Willingness questions are now answered canonically from profile.json.
    """
    return (
        is_relocation_assistance_question(question)
        or is_relocation_timing_question(question)
        or is_remote_willingness_question(question)
    )


# ============================================================================
# Rule 6: Essays / Custom Open-Text / Ratings Matcher
# ============================================================================

ESSAY_PATTERNS = [
    # Why fit / Why join / Why company
    r"why\s*(?:are\s*you\s*(?:a\s*)?good\s*fit|are\s*you\s*interested|do\s*you\s*want\s*to\s*work|do\s*you\s*want\s*to\s*join|join|us\??)",
    r"what\s*makes\s*you\s*want\s*to\s*(?:join|work)",
    r"rank\s*yourself\s*out\s*of\s*\d+",
    r"why\s*(?:do\s*you\s*want\s*to\s*)?(?:join|work\s*at|work\s*for|work\s*with)",
    r"why\s+(?:anthropic|openai|writer|cloudflare|dropbox|shield\s*ai|retell|whoop|figma)",
    r"motivation\s*for\s*applying",
    r"what\s*interests?\s*you\s*about",
    r"what\s*excited\s*you",
    r"why\s*this\s*(?:role|company|position|opportunity)",
    r"tell\s*us\s*why\s*you",

    # Opinion / Story / Papers / Challenges
    r"most\s*interesting\s*(?:[\w\s]{0,20})?(?:paper|blog|article|project|research)",
    r"proudest\s*(?:[\w\s]{0,20})?(?:project|achievement|accomplishment)",
    r"technical\s*challenge",
    r"difficult\s*bug",
    r"challenging\s*(?:technical\s*)?(?:bug|problem|project|issue|challenge)",
    r"(?:explain|describe)\s*(?:a\s*)?(?:challenging|complex|difficult)?\s*(?:technical\s*)?(?:bug|problem|project|challenge|incident)",
    r"production\s*incident",
    r"obstacle\s*(?:you\s*)?overcame",
    r"greatest\s*(?:[\w\s]{0,20})?(?:accomplishment|achievement)",
    r"describe\s*a\s*(?:time|situation|project|challenge)",
    r"tell\s*us\s*about\s*a\s*(?:time|project|bug|challenge)",

    # Mission / Values
    r"mission\s*means",
    r"values\s*align",
    r"what\s*does\s*(?:purpose|mission|economic\s*empowerment)\s*mean",
    r"economic\s*empowerment",

    # Ratings
    r"rate\s*your(?:self)?",
    r"rate\s*your\s*(?:level|proficiency|knowledge|skill|speaking|reading|writing|experience)",
    r"on\s*a\s*scale\s*of\s*(?:1|0)\s*(?:to|-)\s*(?:5|10)",
    r"scale\s*(?:from\s*)?(?:1|0)\s*(?:to|-)\s*(?:5|10)",

    # Salary history narrative
    r"salary\s*history",
    r"narrative\s*regarding\s*compensation",
    r"explain\s*your\s*compensation",
]


def is_essay_or_subjective_question(question: str) -> bool:
    """
    Rule 6: Detects open-text essays, personal stories, opinions, and self-ratings.
    If required, safe-halt with ESSAY_HALT. If optional, leave blank.
    """
    if not question:
        return False
    q = question.strip()
    return any(re.search(pat, q, re.IGNORECASE) for pat in ESSAY_PATTERNS)


# ============================================================================
# Rule 7: Contradictory-Premise Questions Matcher
# ============================================================================

CONTRADICTORY_PREMISE_PATTERNS = [
    # International follow-up when candidate is US-based
    r"if\s*you\s*(?:selected|chose|are)\s*international",
    r"international\s*location\(?s?\)?",
    r"locations?\s*outside\s*(?:of\s*)?(?:the\s*)?u\.?s\.?",

    # Veteran follow-up when candidate is not a veteran
    r"if\s*(?:you\s*are\s*a\s*)?veteran.*(?:specify|branch|discharge|rank)",
    r"military\s*branch.*if\s*applicable",
    r"military\s*discharge\s*status",

    # Disability accommodation follow-up when candidate has no disability
    r"if\s*yes,?\s*(?:please\s*)?describe.*accommodation",
    r"nature\s*of\s*disability.*accommodation",

    # Referral follow-up when candidate applied independently
    r"if\s*(?:you\s*were\s*)?referred.*(?:who|name|specify|list)",
    r"employee\s*referral\s*name",
    r"name\s*of\s*(?:the\s*)?referr",
    r"who\s*referred\s*you",
]


def is_contradictory_premise_question(question: str) -> bool:
    """
    Rule 7: Detects questions whose premise contradicts the candidate profile.
    If required, safe-halt with PREMISE_MISMATCH. If optional, leave blank.
    """
    if not question:
        return False
    q = question.strip()
    return any(re.search(pat, q, re.IGNORECASE) for pat in CONTRADICTORY_PREMISE_PATTERNS)


# ============================================================================
# Rule 8: Home / Street Address Matcher
# ============================================================================

HOME_ADDRESS_PATTERNS = [
    r"\bhome\s*address\b",
    r"\bstreet\s*address\b",
    r"\bresidential\s*address\b",
    r"\baddress\s*line\s*1\b",
    r"\baddress\s*1\b",
    r"\bstreet\s*address\s*line\b",
    r"\bphysical\s*address\b",
]

NOT_HOME_ADDRESS_PATTERNS = [
    r"\bcity\b",
    r"\bstate\b",
    r"\bzip\b",
    r"\bpostal\b",
    r"\bcountry\b",
    r"\bemail\b",
    r"\bip\s*address\b",
]


def is_home_address_field(label: str) -> bool:
    """
    Rule 8: Detects specific street / home address input fields.
    A city, metro, "USA", or workplace is NEVER a home address.
    """
    if not label:
        return False
    lbl = label.strip().lower()
    if any(re.search(p, lbl) for p in NOT_HOME_ADDRESS_PATTERNS):
        return False
    return any(re.search(p, lbl) for p in HOME_ADDRESS_PATTERNS)


# ============================================================================
# Rule 9: "How Did You Hear About Us?" Matcher & Resolver
# ============================================================================

HOW_HEARD_PATTERNS = [
    r"how\s*did\s*you\s*(?:hear|learn|find)",
    r"where\s*did\s*you\s*(?:hear|see|find)",
    r"(?:from\s+)?which\s+(?:job\s+)?(?:site|board|source|platform|website)\s+did\s+you\s+(?:see|find|hear|learn)",
    r"which\s+(?:job\s+)?(?:site|board|source)\s+(?:led|brought)\s+you",
    r"source\s*of\s*application",
    r"referr(?:al|ed)\s*source",
    r"application\s*source",
    r"how\s*did\s*you\s*come\s*across",
]

APPROVED_SOURCE_TIER_1 = [
    "company website", "company site", "careers page", "career page",
    "careers website", "career site", "company careers", "our website",
    "corporate website", "employer website", "direct", "website",
]

APPROVED_SOURCE_TIER_2 = [
    "job board", "job boards", "job posting site", "job site", "online job board",
    "internet job board", "online", "internet",
]

BANNED_SOURCES = [
    "linkedin", "referral", "employee", "agency", "recruiter",
    "social media", "twitter", "x.com", "facebook", "instagram",
    "glassdoor", "indeed", "handshake", "conference", "event",
]


def is_how_did_you_hear_question(question: str) -> bool:
    if not question:
        return False
    return any(re.search(pat, question.strip(), re.IGNORECASE) for pat in HOW_HEARD_PATTERNS)


def solve_how_did_you_hear(question: str, choices: Optional[List[str]] = None, is_required: bool = True) -> str:
    """
    Rule 9: Company Website First. NEVER LinkedIn, NEVER Referral, NEVER Social Media.
    Selection order:
    1. Company Website / Careers Page
    2. Job Board / Job Posting Site
    3. Other + "Company careers page" ONLY if dropdown has no site/board option
    If none match:
    - If required: raises FormHaltException("SOURCE_OPTION_MISSING")
    - If optional: returns ""
    """
    if not choices:
        return "Company careers page"

    # Clean choices
    clean_choices = [c.strip() for c in choices if c and c.strip()]

    # Tier 1: Company Website / Careers Page
    for target in APPROVED_SOURCE_TIER_1:
        for c in clean_choices:
            if target in c.lower():
                return c

    # Tier 2: Job Board / Job Posting Site
    for target in APPROVED_SOURCE_TIER_2:
        for c in clean_choices:
            c_low = c.lower()
            # Guard against banned terms inside choice
            if any(b in c_low for b in ["linkedin", "referral", "agency", "social"]):
                continue
            if target in c_low:
                return c

    # Tier 3: Other
    other_choice = None
    for c in clean_choices:
        if c.lower().strip() in ["other", "other (please specify)", "other / please specify"]:
            other_choice = c
            break
    if other_choice:
        return other_choice

    # None of the approved options match
    if is_required:
        raise FormHaltException(
            "SOURCE_OPTION_MISSING",
            f"No approved referral source option in choices {clean_choices}. Banned: LinkedIn, Referral, Social Media.",
            question=question,
            choices=choices
        )
    return ""


# ============================================================================
# Rule 5: Visa Sponsorship & "Which Visa?" Resolver
# ============================================================================

WHICH_VISA_PATTERNS = [
    r"which\s*visa",
    r"type\s*of\s*visa",
    r"what\s*visa",
    r"sponsorship\s*type",
    r"which\s*sponsorship\s*type",
    r"visa\s*category",
    r"visa\s*classification",
    r"visa\s*status\s*needed",
    r"visa\s*type\s*required",
]


def is_which_visa_question(question: str) -> bool:
    if not question:
        return False
    return any(re.search(pat, question.strip(), re.IGNORECASE) for pat in WHICH_VISA_PATTERNS)


def solve_which_visa(question: str, choices: Optional[List[str]] = None, is_required: bool = True) -> str:
    """
    Rule 5: "Which Visa?" = H-1B.
    If choices present: select H-1B option. If H-1B absent: HALT with VISA_OPTION_MISSING.
    If free-text: return "H-1B".
    """
    if not choices:
        return "H-1B"

    # Search for H-1B match in choices
    for c in choices:
        c_low = c.lower().strip()
        if re.search(r"\bh-?1b\b", c_low):
            return c

    # H-1B option not offered in choices
    raise FormHaltException(
        "VISA_OPTION_MISSING",
        f"H-1B visa option is missing from choices: {choices}. Never guessing alternative visa type.",
        question=question,
        choices=choices
    )


# ============================================================================
# Rule 10: Communications / SMS Consent Resolver
# ============================================================================

SMS_STATUS_PATTERNS = [
    r"application\s*status",
    r"updates?\s*regarding\s*your\s*application",
    r"regarding\s*(?:the\s*|your\s*)application",
    r"text\s*messages?\s*regarding\s*your\s*application",
    r"application\s*updates?",
    r"interview\s*scheduling",
    r"status\s*of\s*your\s*application",
    r"hiring\s*process\s*updates?",
    r"text\s*messages?",
    r"\bsms\b",
]

MARKETING_PATTERNS = [
    r"marketing",
    r"promotions?",
    r"talent\s*community",
    r"talent\s*network",
    r"talent\s*pool",
    r"future\s*(?:job\s*)?openings?",
    r"newsletters?",
    r"promotional",
]


def solve_consent_question(question: str, choices: Optional[List[str]] = None) -> str:
    """
    Rule 10: Fine-grained communications consent.
    - Application status SMS / updates = YES (opt in)
    - Marketing / talent pool / promotions = NO (opt out)
    - Bundled (both in single checkbox) = HALT with CONSENT_AMBIGUOUS
    """
    q_low = question.lower().strip()

    has_status = any(re.search(p, q_low) for p in SMS_STATUS_PATTERNS)
    has_marketing = any(re.search(p, q_low) for p in MARKETING_PATTERNS)

    if has_status and has_marketing:
        # Bundled consent -> HALT
        raise FormHaltException(
            "CONSENT_AMBIGUOUS",
            "Single consent bundles application status with marketing/talent-pool. Cannot agree to marketing or decline status updates.",
            question=question,
            choices=choices
        )

    if has_status:
        # Opt in to application updates
        if choices:
            for c in choices:
                if any(k in c.lower() for k in ["yes", "agree", "consent", "opt-in", "opt in"]):
                    return c
            return choices[0]
        if "text" in q_low or "sms" in q_low:
            return "Yes - I consent to receiving text messages"
        return "Yes"

    if has_marketing:
        # Opt out of marketing / talent community
        if choices:
            for c in choices:
                if any(k in c.lower() for k in ["no", "decline", "opt-out", "opt out", "do not"]):
                    return c
            return choices[-1]
        return "No"

    # Default general legal acknowledgment / privacy terms
    if choices:
        for c in choices:
            if any(k in c.lower() for k in ["agree", "accept", "yes", "i agree", "i accept", "confirmed"]):
                return c
        return choices[0]
    return "Yes"


# ============================================================================
# Rule 4 & 5 Telegram Alert Formatter
# ============================================================================

def format_missing_info_request(code: str, question: str, url: str, company: str, choices: Optional[List[str]] = None) -> str:
    """
    Formats the EXACTLY ONE human-actionable MISSING_INFO_REQUEST message per application.
    Uses plain text (no legacy Telegram Markdown) so URLs with underscores (e.g. job_app),
    questions, and option lists never trigger Telegram HTTP 400 Bad Request errors.
    """
    choices_str = ""
    if choices:
        choices_list = "\n".join(f"  • {c}" for c in choices)
        choices_str = f"\nOptions Offered:\n{choices_list}\n"

    BOT_SIDE_DEFECT_CODES = {
        "BANNER_BLOCKED",
        "OPTION_LABEL_UNREADABLE",
        "TOGGLE_UNREADABLE",
        "EDUCATION_DATE_INCOMPLETE",
    }

    guidance = ""
    if code in BOT_SIDE_DEFECT_CODES:
        guidance = (
            f"🛠️ Bot-Side Defect ({code}) — Action NOT Required From You\n"
            f"This is a bot-side defect; you are NOT expected to answer this alert. "
            f"The application is safely parked and will become retryable once the bot fix deploys."
        )
    elif code == "TRAVEL_DECISION_REQUIRED":
        guidance = "⚠️ Action Required: Travel Requirement Decision Needed\nStanding policy: No canonical travel willingness answer is on file. Halting without guessing."
    elif code == "UNMAPPED_SCREENING_QUESTION":
        guidance = "⚠️ Action Required: Unmapped Screening Question\nStanding policy (No Guessing): No canonical answer exists in profile.json for this question. Halting without guessing."
    elif code == "RELOCATION_DECISION_REQUIRED":
        guidance = "⚠️ Action Required: Relocation / In-Office Decision Needed\nStanding policy: The bot never guesses or assumes your relocation or in-office willingness."
    elif code in ["HUB_NOT_NEAR_RESIDENCE", "PROXIMITY_REQUIREMENT_UNMET"]:
        guidance = "⚠️ Action Required: Office Hub / Proximity Decision Needed\nStanding policy: The bot answers proximity questions truthfully from your profile location (Washington, DC). No DC metro hub or truthful negative/remote option is available among choices."
    elif code == "RELOCATION_ASSISTANCE_HALT":
        guidance = "⚠️ Action Required: Relocation Assistance Question Encountered\nStanding policy: The bot does not store or assume relocation assistance preferences. Halting for human answer."
    elif code == "RELOCATION_TIMING_HALT":
        guidance = "⚠️ Action Required: Relocation Timing Question Encountered\nStanding policy: The bot does not store or assume relocation move timelines. Halting for human answer."
    elif code == "REMOTE_WILLINGNESS_HALT":
        guidance = "⚠️ Action Required: Remote Work Preference Question Encountered\nStanding policy: The bot does not store or assume remote work preferences. Halting for human answer."
    elif code == "VISA_OPTION_MISSING":
        guidance = "⚠️ Action Required: Visa Option Missing\nForm does not offer H-1B among available options. The bot will not guess an incorrect visa type."
    elif code == "ADDRESS_REQUIRED":
        guidance = "⚠️ Action Required: Street Address Required\nStanding policy: The bot never stores or guesses your home street address."
    elif code == "PREMISE_MISMATCH":
        guidance = "⚠️ Action Required: Contradictory Question Premise\nQuestion premise contradicts your profile (e.g. international or veteran branch)."
    elif code == "SOURCE_OPTION_MISSING":
        guidance = "⚠️ Action Required: Application Source Option Missing\nDropdown lacks approved 'Company website' or 'Job board' options (LinkedIn/referral banned)."
    elif code == "CONSENT_AMBIGUOUS":
        guidance = "⚠️ Action Required: Ambiguous Consent Checkbox\nForm bundles application status updates with marketing/promotions in a single checkbox."
    elif code == "ESSAY_HALT":
        guidance = "⚠️ Action Required: Screening Essay / Rating Required\nStanding policy: The bot never generates subjective essays, stories, or proficiency ratings."
    else:
        guidance = f"⚠️ Action Required: Missing Information ({code})"

    footer = (
        "🛠️ Bot-Side Park: You are NOT expected to answer. This posting is parked (max 1 alert / 24h) and becomes retryable once fixed."
        if code in BOT_SIDE_DEFECT_CODES
        else "⏱️ 24-Hour Review Window: Please provide the canonical answer or review directly in browser. Application is safely parked until your response."
    )

    msg = (
        f"{guidance}\n\n"
        f"• Company: {company}\n"
        f"• Posting: {url}\n\n"
        f"Question Verbatim:\n{question}\n"
        f"{choices_str}\n"
        f"{footer}"
    )
    return msg
