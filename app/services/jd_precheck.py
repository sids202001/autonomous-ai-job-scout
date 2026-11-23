"""
JD Pre-Check Eligibility Gate (Nov 22 Rule R6)

Evaluates a job title and description BEFORE launching a browser session:
1. Sponsorship ineligibility (candidate requires H-1B / F-1 STEM OPT sponsorship)
2. Language requirement not on file (candidate speaks English, Hindi, Marathi)
3. Graduation-year window mismatch (candidate graduated MS May 2024, BE Aug 2022)

If any check bars the candidate, returns (False, "JD_INELIGIBLE", reason).
"""

import re
from typing import Optional, Tuple, Dict, Any, Set

DEFAULT_SPOKEN_LANGUAGES: Set[str] = {"english", "hindi", "marathi"}

SPONSORSHIP_BAR_PATTERNS = [
    r"not\s+eligible\s+for\s+(?:immigration|visa|work)\s+sponsorship",
    r"(?:unable|cannot|will\s+not|do\s+not|does\s+not)\s+(?:to\s+)?(?:provide\s+|offer\s+)?(?:immigration\s+|visa\s+)?sponsorship",
    r"no\s+(?:immigration|visa)\s+sponsorship(?:\s+(?:available|offered|provided))?",
    r"without\s+(?:the\s+need\s+for\s+|requiring\s+)?(?:current\s+or\s+future\s+)?(?:immigration\s+|visa\s+)?sponsorship",
    r"sponsorship\s+is\s+not\s+(?:available|offered|provided)",
    r"must\s+be\s+a\s+u\.?s\.?\s*citizen",
    r"u\.?s\.?\s*citizenship\s+(?:is\s+)?required",
    r"active\s+(?:top\s+secret|secret|ts/sci)\s+(?:security\s+)?clearance\s+(?:is\s+)?required",
    r"requires?\s+(?:an?\s+)?active\s+(?:u\.?s\.?\s*)?(?:government\s+)?security\s+clearance",
]

OTHER_LANGUAGES = [
    "spanish", "french", "german", "mandarin", "cantonese", "chinese",
    "japanese", "korean", "portuguese", "arabic", "italian", "russian",
    "dutch", "polish", "turkish", "vietnamese", "hebrew", "swedish",
]

GRAD_WINDOW_TITLE_PATTERNS = [
    r"\b202[678]\b.*(?:grad|new\s*grad|campus|university|college|strategist|developer|engineer|analyst|intern)",
    r"(?:grad|new\s*grad|campus|junior|class\s+of)\b.*\b202[678]\b",
    r"\b(?:summer|fall|spring|winter)\s+202[678]\b",
]

GRAD_WINDOW_DESC_PATTERNS = [
    r"graduat(?:ing|e|ion)\s+(?:between|in)\s+(?:[a-z]+\s+)?202[678]",
    r"expected\s+graduation\s+(?:date\s+)?(?:in\s+|of\s+|between\s+)?(?:[a-z]+\s+)?202[678]",
    r"class\s+of\s+202[678]",
    r"currently\s+enrolled\s+in\s+(?:a|an)\s+(?:undergraduate|bachelor|master|phd|degree)",
    r"pursuing\s+(?:a|an)\s+(?:bachelor|master|bs|ba|ms|phd)\s+degree",
]


def evaluate_jd_eligibility(
    title: str,
    description: str = "",
    profile: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str, str]:
    """
    Returns (is_eligible, code, reason).
    code is "ELIGIBLE" or "JD_INELIGIBLE".
    """
    t = (title or "").strip()
    d = (description or "").strip()
    combined = f"{t}\n{d}"
    combined_low = combined.lower()
    t_low = t.lower()

    # Determine spoken languages from profile if provided
    spoken = set(DEFAULT_SPOKEN_LANGUAGES)
    if profile:
        sc = profile.get("screening_canonical", {}) or {}
        if isinstance(sc.get("languages_spoken"), list):
            spoken = {str(x).strip().lower() for x in sc["languages_spoken"] if str(x).strip()}

    # 1. Sponsorship ineligibility
    for pat in SPONSORSHIP_BAR_PATTERNS:
        m = re.search(pat, combined_low, re.IGNORECASE)
        if m:
            return False, "JD_INELIGIBLE", f"Sponsorship/citizenship bar detected in JD: '{m.group(0)}'"

    # 2. Language requirement not on file
    for lang in OTHER_LANGUAGES:
        if lang in spoken:
            continue
        # Language explicitly in title (e.g., "Spanish speaking", "Bilingual French")
        if re.search(rf"\b{lang}\b", t_low):
            return False, "JD_INELIGIBLE", f"Required language '{lang.title()}' in role title not on candidate record"
        # Language explicitly required in JD
        req_pats = [
            rf"(?:fluent|fluency|proficient|proficiency|bilingual|native)\s+(?:in\s+)?(?:written\s+and\s+spoken\s+)?{lang}\b",
            rf"\b{lang}\s+(?:speaking|fluency|proficiency|is\s+required|required)\b",
            rf"must\s+(?:speak|be\s+fluent\s+in)\s+{lang}\b",
        ]
        for rp in req_pats:
            m = re.search(rp, combined_low, re.IGNORECASE)
            if m:
                return False, "JD_INELIGIBLE", f"Required language '{lang.title()}' detected in JD: '{m.group(0)}'"

    # 3. Graduation-year window mismatch (candidate graduated MS May 2024, BE Aug 2022)
    for pat in GRAD_WINDOW_TITLE_PATTERNS:
        m = re.search(pat, t_low, re.IGNORECASE)
        if m:
            return False, "JD_INELIGIBLE", f"Graduation-year/campus cohort mismatch in title: '{m.group(0)}' (candidate graduated May 2024)"

    for pat in GRAD_WINDOW_DESC_PATTERNS:
        m = re.search(pat, combined_low, re.IGNORECASE)
        if m:
            return False, "JD_INELIGIBLE", f"Graduation-year window mismatch in JD: '{m.group(0)}' (candidate graduated May 2024)"

    return True, "ELIGIBLE", ""
