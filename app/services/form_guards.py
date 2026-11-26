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
import json
import logging
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)


def _load_profile_safe() -> Dict[str, Any]:
    """Load canonical profile.json without crashing if unavailable in isolated unit tests."""
    try:
        from app.services.profile_verifier import load_canonical_profile
        prof = load_canonical_profile()
        if isinstance(prof, dict) and prof:
            return prof
    except Exception:
        pass
    try:
        root_profile = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "profile.json",
        )
        if os.path.exists(root_profile):
            with open(root_profile, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


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


def _pick_choice_no(question: str, choices: Optional[List[str]] = None) -> str:
    if choices:
        for c in choices:
            c_s = str(c).strip()
            if re.match(r"^(?:no|false)\b", c_s, re.IGNORECASE) and not re.search(r"\byes\b", c_s, re.IGNORECASE):
                return c_s
        for c in choices:
            c_s = str(c).strip()
            if re.search(r"own\s+expense|without\s+assistance|do\s+not\s+require", c_s, re.IGNORECASE):
                return c_s
        for c in choices:
            c_s = str(c).strip()
            if re.match(r"^(?:not\s+yet|haven'?t|i\s+haven'?t|have\s+not|i\s+have\s+not|not\s+started)\b", c_s, re.IGNORECASE):
                return c_s
        raise FormHaltException(
            "UNMAPPED_SCREENING_QUESTION",
            f"Canonical answer is 'No' for '{question}', but no 'No' option was found among {choices}.",
            question=question,
            choices=choices,
        )
    return "No"


def _pick_choice_yes(question: str, choices: Optional[List[str]] = None) -> str:
    if choices:
        for c in choices:
            c_s = str(c).strip()
            if re.match(r"^(?:yes|true)\b", c_s, re.IGNORECASE) and not re.search(r"\bno\b", c_s, re.IGNORECASE):
                return c_s
        raise FormHaltException(
            "UNMAPPED_SCREENING_QUESTION",
            f"Canonical answer is 'Yes' for '{question}', but no 'Yes' option was found among {choices}.",
            question=question,
            choices=choices,
        )
    return "Yes"


def _has_boolean_choices(choices: Optional[List[str]]) -> bool:
    if not choices:
        return True
    has_yes = any(re.match(r"^(?:yes|true)\b", str(c).strip(), re.IGNORECASE) for c in choices)
    has_no = any(
        re.match(r"^(?:no|false|not\s+yet|i\s+have\s+not|haven'?t)\b", str(c).strip(), re.IGNORECASE)
        for c in choices
    )
    return has_yes or has_no


def _build_candidate_school_aliases(education: List[Dict[str, Any]]) -> List[Tuple[set, bool]]:
    """Return list of (alias_set, is_current) built dynamically from profile.json['education']."""
    stop = {"of", "the", "at", "in", "for", "and", "&"}
    entries: List[Tuple[set, bool]] = []
    for edu in education or []:
        if not isinstance(edu, dict):
            continue
        school = str(edu.get("school") or "").strip()
        if not school:
            continue
        s_low = school.lower()
        aliases = {s_low}
        # Strip leading "university of " or trailing " university" / " college" / " institute"
        core = re.sub(r"^(?:the\s+)?university\s+of\s+", "", s_low).strip()
        core = re.sub(r"\s+(?:university|college|institute(?:\s+of\s+technology)?)$", "", core).strip()
        if core and len(core) >= 3:
            aliases.add(core)
        if "university of " in s_low:
            aliases.add(f"{core} university")
        words = [w for w in re.findall(r"[a-z]+", s_low) if w not in stop]
        if len(words) >= 2:
            acronym = "".join(w[0] for w in words)
            if len(acronym) >= 2:
                aliases.add(acronym)
        entries.append((aliases, bool(edu.get("is_current", False))))
    return entries


_GENERIC_SCHOOL_PHRASES = {
    "university", "college", "school", "institution", "program", "degree",
    "engineering", "engineering program", "computer science", "information systems",
    "information technology", "data science", "software engineering", "stem",
    "accredited university", "accredited college", "accredited institution",
    "4 year university", "four year university", "4 year college", "four year college",
    "recognized university", "us university", "u s university", "united states",
    "bachelor", "bachelors", "master", "masters", "graduate", "undergraduate",
    "phd", "doctorate", "high school", "secondary school",
}


def solve_education_enrollment_question(
    question: str,
    choices: Optional[List[str]] = None,
    profile: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    General education / enrollment / school-specific yes-no rule driven by profile.json['education']
    and profile.json['screening_canonical']['currently_pursuing_degree'].
    - Current enrollment / about to graduate / pursuing a degree -> No (unless profile shows is_current=True).
    - School-specific enrollment/attendance/graduation/alumni questions (ANY university or program):
      - If named school/program is NOT in candidate's education record -> No.
      - If named school/program IS in candidate's education record:
        - Current enrollment / about to graduate -> Yes iff is_current else No.
        - Past attendance / graduation / alumni -> Yes.
    """
    if not question:
        return None
    q = question.strip()
    q_low = q.lower()

    # Never hijack date/year/free-text school-name questions
    if re.search(r"^\s*(?:when|what\s+year|which\s+(?:school|university|college|institution|degree|major)|what\s+(?:school|university|college|institution|degree|major)|name\s+of\s+(?:your\s+)?(?:school|university|college))\b", q_low):
        return None
    if choices and not _has_boolean_choices(choices):
        return None

    prof = profile if isinstance(profile, dict) else _load_profile_safe()
    edu_list = prof.get("education") or []
    sc = prof.get("screening_canonical") or {}
    is_currently_enrolled = any(isinstance(e, dict) and bool(e.get("is_current", False)) for e in edu_list)
    pursuing_raw = str(sc.get("currently_pursuing_degree", "No")).strip().lower()
    is_pursuing = is_currently_enrolled or pursuing_raw in ("yes", "true", "1")

    # 1. General current-enrollment / about-to-graduate / currently-pursuing-degree check
    current_enrollment_patterns = [
        r"\b(?:currently|presently)\s+(?:enrolled|attending|pursuing|studying)\b",
        r"\babout\s+to\s+graduate\b",
        r"\b(?:are\s+you|will\s+you\s+be)\s+(?:a\s+)?(?:current(?:ly)?\s+)?(?:enrolled\s+)?(?:student|undergrad(?:uate)?|grad(?:uate)?\s+student)\b",
        r"\b(?:are\s+you|will\s+you\s+be)\s+(?:currently\s+)?graduating\s+(?:soon|in\s+20\d{2}|this\s+year|next\s+year|from)\b",
        r"\bcurrently\s+(?:in\s+school|completing\s+(?:a|an|your)\s+degree|working\s+towards?\s+(?:a|an|your)\s+degree)\b",
        r"\b(?:are\s+you\s+)?pursuing\s+(?:a|an|your)\s+(?:bachelor|master|phd|doctorate|degree|undergraduate|graduate)\b",
        r"\bexpect(?:ing|ed)?\s+to\s+graduate\s+(?:in|by|before|between)\b",
    ]
    is_current_q = any(re.search(p, q_low, re.IGNORECASE) for p in current_enrollment_patterns)
    if is_current_q:
        return _pick_choice_yes(q, choices) if is_pursuing else _pick_choice_no(q, choices)

    # 2. School / University / Program-specific yes/no questions (past or general)
    is_yesno_framing = bool(
        re.search(r"\b(?:are\s+you|were\s+you|did\s+you|have\s+you|do\s+you|will\s+you)\b", q_low)
        or (choices and _has_boolean_choices(choices))
    )
    has_academic_cue = bool(
        re.search(
            r"\b(?:enrolled|attend(?:ed|ing)?|graduat(?:e|ed|ing)\s+(?:from|of)|"
            r"alumn(?:us|a|i)|student\s+(?:at|in|of)|degree\s+from|"
            r"engineering\s+program|computer\s+science\s+program|fellowship\s+at|"
            r"program\s+at|stud(?:y|ied|ying)\s+at|affiliated\s+with\s+(?:[\w\s]{0,30})?(?:university|college|institute|school))\b",
            q_low,
        )
    )
    if not (is_yesno_framing and has_academic_cue):
        return None

    # Extract named school/program phrase(s) from the original-case question
    named_candidates: List[str] = []
    # Pattern A: preposition + optional article + capitalized proper-noun institution/program phrase
    for m in re.finditer(
        r"\b(?:at|from|of|in)\s+(?:the\s+|a\s+|an\s+)?"
        r"([A-Z][A-Za-z0-9&.'\-]+(?:\s+(?:of|for|at|in|the|&|[A-Z][A-Za-z0-9&.'\-]+)){0,6})",
        q,
    ):
        named_candidates.append(m.group(1).strip(" ?,.:;()\"'"))
    # Pattern B: "attended <Proper Noun>" / "attend <Proper Noun>"
    for m in re.finditer(
        r"\battend(?:ed|ing)?\s+(?:the\s+|a\s+|an\s+)?"
        r"([A-Z][A-Za-z0-9&.'\-]+(?:\s+(?:of|for|at|in|the|&|[A-Z][A-Za-z0-9&.'\-]+)){0,6})",
        q,
    ):
        named_candidates.append(m.group(1).strip(" ?,.:;()\"'"))

    # Filter out generic / non-specific phrases (e.g. "United States", "Computer Science", "Accredited University")
    specific_schools: List[str] = []
    for cand in named_candidates:
        norm = re.sub(r"[^a-z0-9\s]", " ", cand.lower())
        norm = re.sub(r"\s+", " ", norm).strip()
        if not norm or norm in _GENERIC_SCHOOL_PHRASES:
            continue
        # Skip generic "accredited ... university" or "4-year ... college"
        if re.search(r"\b(?:accredited|recognized|four[- ]year|4[- ]year|us|u\.s\.)\b", norm):
            continue
        # Require either an academic suffix, an acronym/school token (e.g. UPenn, MIT, CMU, NYU, UC ...),
        # or explicit "university/college/institute/school/program" context
        has_school_signal = bool(
            re.search(r"\b(?:university|college|institute|school|tech|polytechnic|academy|program)\b", norm)
            or re.search(r"\b(?:upenn|penn|mit|cmu|nyu|ucla|usc|ucsd|ucsb|uiuc|umich|caltech|stanford|harvard|yale|princeton|columbia|cornell|berkeley|brown|dartmouth|duke|northwestern|waterloo|gmu|mumbai|ut|uw|eth|iit)\b", norm)
            or re.match(r"^[A-Z]{2,6}\b", cand)
            or re.match(r"^U[A-Z][a-z]+", cand)
        )
        if has_school_signal:
            specific_schools.append(norm)

    if not specific_schools:
        return None

    school_aliases = _build_candidate_school_aliases(edu_list)
    for spec in specific_schools:
        for aliases, is_curr in school_aliases:
            for alias in aliases:
                if alias == spec or re.search(rf"(?:^|\b){re.escape(alias)}(?:\b|$)", spec):
                    return _pick_choice_yes(q, choices)

    # Specific school/program named, and it is NOT in candidate's education record -> No
    return _pick_choice_no(q, choices)


# ============================================================================
# General Current Visa / Immigration Status Yes-No Solver (profile.json driven)
# ============================================================================

_VISA_STATUS_TOKEN_PATTERNS: List[Tuple[str, str]] = [
    ("stem_opt", r"\bstem[\s-]*opt\b"),
    ("opt", r"\bopt\b"),
    ("cpt", r"\bcpt\b"),
    ("f1", r"\bf-?1\b"),
    ("h1b", r"\bh-?1b1?\b"),
    ("j1", r"\bj-?1\b"),
    ("o1", r"\bo-?1[ab]?\b"),
    ("l1", r"\bl-?1[ab]?\b"),
    ("tn", r"\btn(?:\s+visa|\s+status)?\b"),
    ("e3", r"\be-?3\b"),
    ("e2", r"\be-?2\b"),
    ("h4", r"\bh-?4(?:\s*ead)?\b"),
    ("b1_b2", r"\bb-?[12]\b"),
    ("daca", r"\bdaca\b"),
    ("tps", r"\btps\b"),
    ("us_citizen", r"\bu\.?s\.?\s*citizen\b|\bunited\s+states\s+citizen\b"),
    ("green_card", r"\bgreen\s*card\b|\bpermanent\s+resident\b|\blawful\s+permanent\s+resident\b"),
]


def _extract_visa_status_codes(text: str) -> set:
    if not text:
        return set()
    low = text.lower()
    found = set()
    for code, pat in _VISA_STATUS_TOKEN_PATTERNS:
        if re.search(pat, low, re.IGNORECASE):
            found.add(code)
    return found


def solve_current_visa_status_question(
    question: str,
    choices: Optional[List[str]] = None,
    profile: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    General current visa / immigration status yes-no rule driven by profile.json['compliance_and_eeo']['visa_status'].
    - If a yes/no question asks about current visa/immigration status and names a status matching
      profile.json visa_status (e.g. F-1 / OPT / STEM OPT) -> Yes.
    - If it names a different status (e.g. H-1B, J-1, O-1, L-1, TN, CPT, U.S. citizen, green card)
      not present in profile.json visa_status -> No.
    - Never hijacks future sponsorship questions ('Will you require H-1B sponsorship?'),
      'Which visa?' questions (Rule 5 -> H-1B), expiration dates, or non-boolean status dropdowns.
    """
    if not question:
        return None
    q = question.strip()
    q_low = q.lower()

    # Never hijack future sponsorship, "which/what visa", expiration, or visa refusal questions
    if re.search(
        r"\b(?:sponsor|sponsorship|will\s+you\s+(?:now\s+or\s+in\s+the\s+future\s+)?(?:require|need)|"
        r"in\s+the\s+future|which\s+visa|what\s+visa|type\s+of\s+visa|visa\s+type|visa\s+category|"
        r"expir|refused|denied)\b",
        q_low,
    ):
        return None

    # Only handle yes/no questions (free-text/boolean choices)
    if choices and not _has_boolean_choices(choices):
        return None

    is_yesno_framing = bool(
        re.search(r"\b(?:are\s+you|do\s+you|is\s+your)\b", q_low)
        or (choices and _has_boolean_choices(choices))
    )
    has_current_status_cue = bool(
        re.search(
            r"\b(?:currently|presently)\s+(?:in|on|a|an|holding|have|under|authorized\s+(?:on|under))\b|"
            r"\b(?:in|on)\s+(?:an?\s+)?(?:f-?1|h-?1b1?|j-?1|o-?1[ab]?|l-?1[ab]?|tn|e-?[23]|h-?4|opt|stem[\s-]*opt|cpt|daca|tps)\s*(?:visa|status|ead)?\b|"
            r"\b(?:hold|have)\s+(?:an?\s+)?(?:current\s+)?(?:f-?1|h-?1b1?|j-?1|o-?1[ab]?|l-?1[ab]?|tn|e-?[23]|h-?4|opt|stem[\s-]*opt|cpt|daca|tps|green\s*card)\s*(?:visa|status|ead)?\b|"
            r"\bare\s+you\s+(?:currently\s+)?(?:a\s+)?(?:u\.?s\.?\s*citizen|united\s+states\s+citizen|permanent\s+resident|green\s*card\s+holder)\b",
            q_low,
        )
    )
    if not (is_yesno_framing and has_current_status_cue):
        return None

    asked_codes = _extract_visa_status_codes(q_low)
    if not asked_codes:
        return None

    prof = profile if isinstance(profile, dict) else _load_profile_safe()
    comp = prof.get("compliance_and_eeo") or {}
    profile_visa = str(comp.get("visa_status") or "").strip()
    owned_codes = _extract_visa_status_codes(profile_visa)
    if comp.get("is_us_person") is True or str(comp.get("citizenship") or "").strip().lower() in ("us", "usa", "united states"):
        owned_codes.add("us_citizen")

    if asked_codes.intersection(owned_codes):
        return _pick_choice_yes(q, choices)
    return _pick_choice_no(q, choices)


# ============================================================================
# Unified Conditional Follow-Up Matcher & Solver (Not-Applicable / False Parent)
# ============================================================================

# Exclusions: conditional-looking questions whose condition IS TRUE or handled by a specific canonical rule
_CONDITIONAL_EXCLUSION_PATTERNS = [
    r"work\s*authorization.*expir|authorization\s*expir|opt.*expir",
    r"relocation\s+assistance\s+if",
    r"(?:willing|open|able)\s+to\s+relocate",
    r"if\s+this\s+position\s+is\s+located",
    r"if\s+you\s+selected\s+[\"']?(?:a\s+protected\s+individual|other)[\"']?.*(?:citizenship|visa|above|question)",
    r"state\s+your\s+current\s+citizenship.*visa",
    r"if\s+you\s+(?:selected|chose|are)\s+international",
]

# Explicit false-condition follow-up patterns (candidate is NOT a referral, NOT a veteran,
# NOT disabled, NOT terminated, NOT a former employee/relative/non-compete/criminal, etc.)
CONDITIONAL_FOLLOWUP_PATTERNS = [
    # Referral / referrer follow-ups (candidate NEVER applies via employee referral)
    r"\bif\s+(?:you\s+)?(?:are|were)\s+(?:a\s+)?referr",
    r"\bif\s+referr(?:al|ed)\b",
    r"\bwho\s+referred\s+you\b",
    r"\b(?:employee|internal|staff|team)\s+referr(?:al|er)\s*(?:name|email|details|info)\b",
    r"\bname\s+of\s+(?:the\s+|your\s+)?referr(?:al|er|ing\s+(?:employee|person|contact))\b",
    r"\breferr(?:er|al)'?s?\s+(?:full\s+)?(?:name|email)\b",
    r"\breferred\s+by\s+(?:an?\s+employee|whom|who)\b",
    r"\bplease\s+(?:mention|enter|provide|list|share|specify|state|include)\s+(?:the\s+|their\s+|your\s+)?referr(?:al|er)(?:'s)?\s*(?:name|email)?\b",
    r"\bformer\s+employee\b.*name",
    # Veteran / military branch follow-ups (candidate is not a veteran)
    r"\bif\s+(?:you\s+are\s+(?:a\s+)?)?veteran\b.*(?:specify|branch|discharge|rank|dates?|explain|provide)",
    r"\bmilitary\s+branch\b.*if\s+applicable",
    r"\bmilitary\s+discharge\s+status\b",
    # Disability accommodation follow-ups (candidate has no disability)
    r"\bif\s+yes,?\s*(?:please\s*)?(?:describe|explain|specify|provide|list).*accommodation",
    r"\bnature\s+of\s+disability\b",
    # Terminated / fired / disciplinary / criminal / non-compete / conflict / relative follow-ups
    r"\bif\s+(?:you\s+(?:have\s+)?(?:ever\s+)?been\s+)?(?:terminated|fired|dismissed|asked\s+to\s+resign|disciplined|convicted)\b",
    r"\bif\s+(?:you\s+)?(?:have\s+)?(?:a\s+)?(?:relative|family\s+member|conflict\s+of\s+interest|non-?compete|security\s+clearance|previously\s+(?:worked|been\s+employed))\b.*(?:name|explain|specify|detail|describe|list|provide|mention|company|when|department)",
    # Generic conditional follow-ups ("If yes, please...", "If you chose X, please specify...", "If other, please specify...")
    r"^\s*if\s+(?:yes|so|applicable)\b",
    r"\bif\s+you\s+answered\s+[\"']?yes[\"']?\b",
    r"\bplease\s+(?:explain|specify|describe|provide|detail|list|mention|tell)\s+if\s+(?:yes|applicable|so)\b",
    r"\bif\s+(?:you\s+)?(?:chose|selected|checked)\b.*(?:specify|explain|describe|mention|name|list|provide|detail|tell)",
    r"\bif\s+other,?\s*(?:please\s*)?(?:specify|explain|describe|mention|enter|provide|tell)",
]

# Standalone core profile fields that must NEVER be treated as a parent-dependent N/A follow-up
_CORE_STANDALONE_FIELD_RE = re.compile(
    r"\b(?:first\s*name|last\s*name|full\s*name|legal\s*name|preferred\s*name|email|phone|mobile|cell|"
    r"address|city|state|zip|postal|country|location|linkedin|github|portfolio|website|"
    r"current\s*(?:company|employer|title|role|position)|most\s*recent\s*(?:company|employer|title)|"
    r"school|university|college|degree|major|discipline|gpa|graduation|"
    r"salary|compensation|hourly|start\s*date|notice\s*period|"
    r"authorized\s*to\s*work|work\s*authorization|sponsorship|visa|"
    r"gender|race|ethnicity|veteran\s*status|disability\s*status|sexual\s*orientation|pronouns|hispanic|latino)\b",
    re.IGNORECASE,
)

# Short follow-up prompt cues when paired with a negative parent answer
_DEPENDENT_FOLLOWUP_CUE_RE = re.compile(
    r"(?:^\s*please\s+(?:tell|explain|specify|provide|list|mention|describe|state|enter|share|indicate|elaborate)\b|"
    r"\b(?:tell|provide|enter|mention|list|specify|state)\s+(?:the\s+|their\s+|your\s+)?(?:company\s*name|employer\s*name|name|details|reason|dates?|relationship|branch)\b|"
    r"^\s*(?:company\s*name|employer\s*name|which\s*company|who|their\s*name|name\s*of\s*company|details|explanation|reason|if\s*yes|if\s*so|if\s*applicable)\s*[?:.]*\s*$)",
    re.IGNORECASE,
)

_NEGATIVE_PARENT_ANSWER_RE = re.compile(
    r"^(?:no|false|never|none|n/a|not\s+applicable|i\s+(?:am|have)\s+not|no,)\b",
    re.IGNORECASE,
)


def is_conditional_followup_question(
    question: str,
    parent_question: Optional[str] = None,
    parent_answer: Optional[str] = None,
) -> bool:
    """
    General detector for conditional follow-up fields whose parent condition does NOT apply to the candidate.
    Works both statelessly (from the question text itself) and contextually (from the immediately preceding
    parent question + answer on the same form, e.g. 'Have you ever been terminated?' -> 'No', followed by
    'Please tell company name').
    """
    if not question:
        return False
    q = question.strip()
    q_low = q.lower()

    # Never hijack "How did you hear / referral source" (Rule 9)
    if is_how_did_you_hear_question(q):
        return False

    # Never hijack exclusions (work auth expiration, relocation willingness, export control citizenship, Rule 7 international)
    if any(re.search(pat, q_low, re.IGNORECASE) for pat in _CONDITIONAL_EXCLUSION_PATTERNS):
        return False

    # Never treat sponsorship/visa detail follow-up as false condition (candidate DOES require sponsorship!)
    if re.search(r"\b(?:sponsorship|visa)\b", q_low) and re.search(r"\bif\s+(?:yes|you\s+(?:require|need|will\s+require))\b", q_low):
        return False

    # 1. Stateless conditional follow-up match
    if any(re.search(pat, q_low, re.IGNORECASE) for pat in CONDITIONAL_FOLLOWUP_PATTERNS):
        return True

    # 2. Context-aware parent-question match (e.g., parent "Have you ever been terminated?" -> "No",
    #    follow-up "Please tell company name")
    if parent_question and parent_answer is not None:
        pa = str(parent_answer).strip()
        pq_low = str(parent_question).strip().lower()
        parent_is_negative = bool(_NEGATIVE_PARENT_ANSWER_RE.match(pa))
        parent_is_non_referral_source = (
            is_how_did_you_hear_question(parent_question)
            and "referral" not in pa.lower()
            and "employee" not in pa.lower()
        )
        if (parent_is_negative or parent_is_non_referral_source) and not _CORE_STANDALONE_FIELD_RE.search(q_low):
            if _DEPENDENT_FOLLOWUP_CUE_RE.search(q_low):
                return True
            if parent_is_non_referral_source and re.search(r"\breferr(?:al|er)\b|\btheir\s+name\b", q_low):
                return True
            if parent_is_negative and re.search(r"(?:terminat|fired|dismiss|disciplin|convict|non-?compet|conflict|relative|clearance|veteran|disabilit|previously\s+(?:employed|worked))", pq_low):
                if len(q_low) <= 120 and re.search(r"\b(?:company|employer|name|explain|detail|reason|specify|when|date|who|which|describe|provide|tell|list|mention)\b", q_low):
                    return True

    return False


def solve_conditional_followup(
    question: str,
    choices: Optional[List[str]] = None,
    is_required: bool = True,
    parent_question: Optional[str] = None,
    parent_answer: Optional[str] = None,
) -> Optional[str]:
    """
    Resolves conditional follow-up fields whose parent condition is false/not-applicable:
    - Optional (no asterisk): return "" (leave blank / skip).
    - Required (with asterisk): return "N/A" (or matching N/A / Not applicable / None option if choices exist).
    Never halts.
    """
    if not question:
        return None
    q_low = question.strip().lower()

    # Special positive conditional: sponsorship detail ("If yes, please explain your sponsorship/visa needs")
    if (
        re.search(r"\b(?:sponsorship|visa)\b", q_low)
        and re.search(r"\bif\s+(?:yes|you\s+(?:require|need|will\s+require))\b", q_low)
        and not choices
    ):
        prof = _load_profile_safe()
        sc = prof.get("screening_canonical") or {}
        return str(sc.get("sponsorship_detail") or "F-1 OPT STEM; will require H-1B sponsorship in the future.")

    if not is_conditional_followup_question(question, parent_question=parent_question, parent_answer=parent_answer):
        return None

    if not is_required:
        return ""

    if choices:
        for pat in (r"^n/?a$", r"not\s+applicable", r"none\s+of\s+the\s+above", r"^none$", r"^no\b"):
            for c in choices:
                c_s = str(c).strip()
                if re.search(pat, c_s, re.IGNORECASE):
                    return c_s
    return "N/A"


def solve_canonical_screening_question(
    question: str,
    choices: Optional[List[str]] = None,
    is_required: bool = True,
    parent_question: Optional[str] = None,
    parent_answer: Optional[str] = None,
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

    # 4c. Education / current enrollment / school-specific yes-no questions (driven by profile.json['education'])
    edu_ans = solve_education_enrollment_question(q, choices)
    if edu_ans is not None:
        return edu_ans

    # 4c2. Current visa / immigration status yes-no questions (driven by profile.json['compliance_and_eeo']['visa_status'])
    visa_status_ans = solve_current_visa_status_question(q, choices)
    if visa_status_ans is not None:
        return visa_status_ans

    # 4d. Conditional follow-up fields whose parent condition is false/not-applicable
    #     (optional -> "" / skip; required -> "N/A"; never halts)
    cond_ans = solve_conditional_followup(
        q,
        choices,
        is_required=is_required,
        parent_question=parent_question,
        parent_answer=parent_answer,
    )
    if cond_ans is not None:
        return cond_ans

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
    # International follow-up when candidate is US-based (Rule 7 safe-halt on required)
    r"if\s*you\s*(?:selected|chose|are)\s*international",
    r"international\s*location\(?s?\)?",
    r"locations?\s*outside\s*(?:of\s*)?(?:the\s*)?u\.?s\.?",
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
