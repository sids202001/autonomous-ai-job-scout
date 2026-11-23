"""
AutoApply - Universal Experience Filter Gate (Rule 11)
Enforces the hard 0–3 years experience requirement BEFORE any application work.

Rules:
- Apply ONLY to postings requiring 0–3 years experience.
- PASS: entry-level, "0–2", "1–3", "2–4" ranges.
- SKIP: anything requiring 4+ years or senior/staff/principal-level seniority.
- Parse "X+ years", "X–Y years", "minimum X years", "X to Y years" robustly.
- Strip HTML tags and decode entities before running regex.
- Exclude hardware/facilities role families (Cloudflare data center hardware bug).
- Log every skip with parsed requirement vs the 3-year cap.
"""

import re
import html
import logging
from typing import Tuple, Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# Seniority titles that immediately disqualify a posting
SENIOR_TITLE_PATTERNS = [
    r"\bsenior\b",
    r"\bsr\.?\b",
    r"\bstaff\b",
    r"\bprincipal\b",
    r"\blead\b",
    r"\bdirector\b",
    r"\bhead\s+of\b",
    r"\bvp\b",
    r"\bvice\s*president\b",
    r"\bpresident\b",
    r"\bchief\b",
    r"\bc[a-z]o\b",
    r"\bexecutive\b(?!\s*assistant)",
    r"\barchitect\b",
    r"\bmanager\b",
    r"\btech\s*lead\b",
    r"\blevel\s*[4-9]\b",
    r"\b(?:l|e|ic)[5-9]\b",
    r"\b(?:iii|iv|v|vi)\b",  # Roman numerals III, IV, V (e.g. Software Engineer III)
]

# Hardware, Data Center, Facilities, and Field Ops roles (disqualified from software/data scope)
ROLE_FAMILY_EXCLUSION_PATTERNS = [
    r"\bdata\s*center\s*(?:engineer|technician|specialist|ops|operations|lead|tech)\b",
    r"\bdatacenter\s*(?:engineer|technician|specialist|ops|operations|lead|tech)\b",
    r"\bdc\s*technician\b",
    r"\bcable\s*(?:[\w\s]{0,20})?(?:technician|engineer|installer)\b",
    r"\bcable\s*plant\b",
    r"\bcabling\b",
    r"\brack\s*(?:and\s*stack|engineer|technician)\b",
    r"\bfacilities\s*(?:[\w\s]{0,20})?(?:engineer|technician|manager|operations|specialist)\b",
    r"\bhvac\b",
    r"\bfacilities\b",
    r"\bfield\s*(?:engineer|technician|operations|service|ops)\b",
    r"\bhardware\s*(?:[\w\s]{0,25})?(?:engineer|technician|support|specialist|lead|ops|operations)\b",
    r"\bhardware\s*infrastructure\b",
    r"\bit\s*support\b",
    r"\bdesktop\s*support\b",
    r"\bhelpdesk\b",
    r"\bnoc\s*technician\b",
    r"\btelecommunications\s*technician\b",
    r"\boptical\s*technician\b",
]

# Entry-level signals that confirm 0-year or early-career suitability
ENTRY_LEVEL_PATTERNS = [
    r"\bentry[\s\-]*level\b",
    r"\bjunior\b",
    r"\bjr\.?\b",
    r"\bassociate\b",
    r"\bnew[\s\-]*grad(?:uate)?\b",
    r"\buniversity[\s\-]*grad(?:uate)?\b",
    r"\bearly[\s\-]*career\b",
    r"\bintern(?:ship)?\b",
    r"\bco[\s\-]*op\b",
    r"\bapprentice(?:ship)?\b",
    r"\bcollege[\s\-]*grad(?:uate)?\b",
]

# Number words to digit mapping
WORD_TO_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10
}


def clean_jd_text(text: str) -> str:
    """Strip HTML tags, decode HTML entities, and normalize whitespace."""
    if not text:
        return ""
    # Strip HTML tags
    cleaned = re.sub(r"<[^>]+>", " ", text)
    # Decode HTML entities (&nbsp;, &#39;, &amp;, etc.)
    cleaned = html.unescape(cleaned)
    # Replace non-breaking spaces and collapse whitespace
    cleaned = re.sub(r"[\s\xa0]+", " ", cleaned).strip()
    return cleaned


class ExperienceGate:
    """
    Hard gate validating experience requirement against the 0-3 years cap.
    """

    MAX_YEARS_CAP = 3

    @classmethod
    def evaluate(cls, title: str, description: str, allow_unstated: Optional[bool] = None) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Evaluate if a job posting satisfies the 0-3 years experience requirement.

        Returns:
            (is_eligible: bool, decision_reason: str, metadata: dict)
        """
        title_str = (title or "").strip()
        raw_desc = (description or "").strip()
        clean_desc = clean_jd_text(raw_desc)
        combined_text = f"{title_str} {clean_desc}"

        metadata: Dict[str, Any] = {
            "title": title_str,
            "parsed_requirement": None,
            "min_years": None,
            "max_years": None,
            "is_entry_level": False,
            "cap": cls.MAX_YEARS_CAP,
        }

        # Step 0: Check role-family exclusions (Hardware, Data Center Technician, Facilities, Field Ops)
        for pat in ROLE_FAMILY_EXCLUSION_PATTERNS:
            if re.search(pat, title_str, re.IGNORECASE):
                reason = f"ROLE_FAMILY_EXCLUDED: Title '{title_str}' matches excluded hardware/facilities/field-ops family ({pat})"
                logger.info(reason)
                return False, reason, metadata
            if re.search(pat, combined_text, re.IGNORECASE) and not any(re.search(p, title_str, re.IGNORECASE) for p in [r"\bsoftware\b", r"\bdata\b", r"\bbackend\b"]):
                reason = f"ROLE_FAMILY_EXCLUDED: Role matches excluded hardware/facilities domain ({pat})"
                logger.info(reason)
                return False, reason, metadata

        # Step 1: Check title seniority
        for pat in SENIOR_TITLE_PATTERNS:
            if re.search(pat, title_str, re.IGNORECASE):
                reason = f"SENIORITY_EXCEEDED: Title '{title_str}' indicates senior/staff/principal seniority (cap: 0-{cls.MAX_YEARS_CAP} yrs)"
                logger.info(f"ExperienceGate reject: {reason}")
                return False, reason, metadata

        # Step 2: Check for explicit entry-level declaration in title or description
        has_entry_level_title = any(re.search(p, title_str, re.IGNORECASE) for p in ENTRY_LEVEL_PATTERNS)
        has_entry_level_desc = any(re.search(p, clean_desc, re.IGNORECASE) for p in ENTRY_LEVEL_PATTERNS)

        # Step 3: Scan all experience requirements from cleaned description
        # If ANY requirement specifies >= 4 years (or > cap), it is a hard skip!
        parsed_reqs = cls._parse_all_experience_requirements(clean_desc)

        if parsed_reqs:
            # Check if any requirement exceeds the cap
            for min_y, max_y, raw_expr in parsed_reqs:
                if max_y is not None:
                    # Range like "3-5", "4-6" -> min_y > 2 or max_y > 4 fails
                    if not (min_y <= 2 and max_y <= 4) and not (min_y <= cls.MAX_YEARS_CAP and max_y <= cls.MAX_YEARS_CAP):
                        metadata["min_years"] = min_y
                        metadata["max_years"] = max_y
                        metadata["parsed_requirement"] = raw_expr
                        reason = f"EXPERIENCE_EXCEEDED: Parsed requirement '{raw_expr}' (min: {min_y}, max: {max_y}) exceeds 0-{cls.MAX_YEARS_CAP} yrs cap"
                        logger.info(reason)
                        return False, reason, metadata
                else:
                    # Single value like "4+ years" or "minimum 5 years"
                    if min_y > cls.MAX_YEARS_CAP:
                        metadata["min_years"] = min_y
                        metadata["parsed_requirement"] = raw_expr
                        reason = f"EXPERIENCE_EXCEEDED: Parsed requirement '{raw_expr}' ({min_y} yrs) exceeds 0-{cls.MAX_YEARS_CAP} yrs cap"
                        logger.info(reason)
                        return False, reason, metadata

            # If we reached here, at least one requirement was found and none exceeded cap
            first_min, first_max, first_raw = parsed_reqs[0]
            metadata["min_years"] = first_min
            metadata["max_years"] = first_max
            metadata["parsed_requirement"] = first_raw
            reason = f"EXPERIENCE_PASSED: Parsed requirement '{first_raw}' satisfies 0-{cls.MAX_YEARS_CAP} yrs cap"
            logger.info(reason)
            return True, reason, metadata

        # Step 4: If no explicit year range found, check if title or desc explicitly declared entry-level
        if has_entry_level_title or (has_entry_level_desc and not re.search(r"\d+\+?\s*years?", clean_desc, re.IGNORECASE)):
            metadata["is_entry_level"] = True
            metadata["min_years"] = 0
            metadata["max_years"] = 1
            metadata["parsed_requirement"] = "Entry-Level / Early Career Declaration"
            reason = f"EXPERIENCE_PASSED: Verified entry-level / early career designation (0-{cls.MAX_YEARS_CAP} yrs)"
            logger.info(reason)
            return True, reason, metadata

        # Step 5: No explicit experience requirement found.
        # If ALLOW_UNSTATED_EXPERIENCE is enabled (and title already passed
        # seniority check in Step 1), treat as entry-level compatible.
        if allow_unstated is None:
            try:
                from config import settings
                allow_unstated = getattr(settings, "ALLOW_UNSTATED_EXPERIENCE", True)
            except Exception:
                allow_unstated = True

        if allow_unstated:
            metadata["parsed_requirement"] = "No experience stated (unstated-pass)"
            metadata["min_years"] = 0
            metadata["max_years"] = None
            metadata["is_entry_level"] = False
            reason = (
                f"UNSTATED_EXPERIENCE_PASS: No verifiable experience requirement found. "
                f"Title '{title_str}' has no seniority signal → proceeding under ALLOW_UNSTATED_EXPERIENCE toggle."
            )
            logger.info(reason)
            return True, reason, metadata

        # Strict mode: Unparseable requirement → Hard Skip. Never guess eligibility!
        reason = f"UNPARSEABLE_EXPERIENCE: Posting does not specify a verifiable 0-{cls.MAX_YEARS_CAP} years requirement. Never guessing eligibility."
        logger.info(reason)
        return False, reason, metadata

    @classmethod
    def _parse_experience_requirement(cls, text: str) -> Optional[Tuple[int, Optional[int], str]]:
        """Extract primary years-of-experience requirement from text."""
        reqs = cls._parse_all_experience_requirements(text)
        return reqs[0] if reqs else None

    @classmethod
    def _parse_all_experience_requirements(cls, text: str) -> List[Tuple[int, Optional[int], str]]:
        """
        Extract ALL years-of-experience requirements from text.
        Returns list of (min_years, max_years, raw_matched_string).
        """
        if not text:
            return []

        cleaned = clean_jd_text(text)
        results: List[Tuple[int, Optional[int], str]] = []
        matched_spans: List[Tuple[int, int]] = []

        def _is_overlapping(span: Tuple[int, int]) -> bool:
            return any(not (span[1] <= s[0] or span[0] >= s[1]) for s in matched_spans)

        # Pattern 1: Ranges like "0-2 years", "1 to 3 years", "2–4 years", "1-3 years of Python experience"
        range_pat = re.compile(
            r"(\d+)\s*(?:-|–|—|to)\s*(\d+)\+?\s*(?:years?|yrs?)"
            r"(?:\s+(?:of\s+)?[\w\s\-/]{1,35}?\s*(?:experience|\bexp\b))?",
            re.IGNORECASE
        )
        for m in range_pat.finditer(cleaned):
            if not _is_overlapping(m.span()):
                matched_spans.append(m.span())
                results.append((int(m.group(1)), int(m.group(2)), m.group(0).strip()))

        # Pattern 2: "X+ years", "X or more years", "X plus years"
        plus_pat = re.compile(
            r"(\d+)\s*(?:\+|plus|\s+or\s+more)\s*(?:years?|yrs?)"
            r"(?:\s+(?:of\s+)?[\w\s\-/]{1,35}?\s*(?:experience|\bexp\b))?",
            re.IGNORECASE
        )
        for m in plus_pat.finditer(cleaned):
            if not _is_overlapping(m.span()):
                matched_spans.append(m.span())
                results.append((int(m.group(1)), None, m.group(0).strip()))

        # Pattern 3: "minimum X years", "at least X years", "minimum of X years"
        min_pat = re.compile(
            r"(?:minimum(?:\s+of)?|at\s+least|\bmin\.?)\s*(\d+)\+?\s*(?:years?|yrs?)"
            r"(?:\s+(?:of\s+)?[\w\s\-/]{1,35}?\s*(?:experience|\bexp\b))?",
            re.IGNORECASE
        )
        for m in min_pat.finditer(cleaned):
            if not _is_overlapping(m.span()):
                matched_spans.append(m.span())
                results.append((int(m.group(1)), None, m.group(0).strip()))

        # Pattern 4: "X years of experience" or "X years [qualifier] experience"
        simple_pat = re.compile(
            r"(\d+)\s*(?:years?|yrs?)(?:\s+(?:of\s+)?[\w\s\-/]{1,35}?\s*(?:experience|\bexp\b))",
            re.IGNORECASE
        )
        for m in simple_pat.finditer(cleaned):
            if not _is_overlapping(m.span()):
                matched_spans.append(m.span())
                results.append((int(m.group(1)), None, m.group(0).strip()))

        # Pattern 5: Spelled out numbers ("four+ years", "five years of experience")
        words = "|".join(WORD_TO_NUM.keys())
        word_single_pat = re.compile(
            rf"(?:minimum(?:\s+of)?|at\s+least)?\s*({words})\+?\s*years?(?:\s+of)?\s*experience",
            re.IGNORECASE
        )
        for m in word_single_pat.finditer(cleaned):
            if not _is_overlapping(m.span()):
                matched_spans.append(m.span())
                w = m.group(1).lower()
                results.append((WORD_TO_NUM[w], None, m.group(0).strip()))

        return results


# Singleton helper
experience_gate = ExperienceGate()
