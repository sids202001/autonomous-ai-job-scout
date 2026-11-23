"""
AutoApply - Universal Common Field Coverage & Learned Registry
Resolves common job application fields (years of experience, notice period, compensation,
location, remote willingness, profile links) across all ATS platforms (Lever, Greenhouse, Ashby, LinkedIn).
Includes persistent learning from user-answered manual reviews.
"""
import os
import re
import json
import logging
from typing import Optional, List, Dict, Any

try:
    from config import settings, COMMON_FIELDS
    if not hasattr(settings, "DATA_DIR"):
        raise AttributeError("settings missing DATA_DIR")
except Exception:
    import importlib.util
    _p = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "config.py"))
    _spec = importlib.util.spec_from_file_location("sid_job_scrip_config", _p)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    settings = _mod.settings
    COMMON_FIELDS = _mod.COMMON_FIELDS

logger = logging.getLogger(__name__)

LEARNED_FIELDS_FILE = os.path.join(settings.DATA_DIR, "learned_fields.json")


class CommonFieldResolver:
    """Universal resolver for standard and learned application questions."""

    def __init__(self, learned_file: str = LEARNED_FIELDS_FILE):
        self.learned_file = learned_file
        self.learned_cache: Dict[str, str] = {}
        self._load_learned_fields()

    def _load_learned_fields(self):
        try:
            if os.path.exists(self.learned_file):
                with open(self.learned_file, "r", encoding="utf-8") as f:
                    self.learned_cache = json.load(f)
        except Exception as e:
            logger.debug(f"Could not load learned fields: {e}")
            self.learned_cache = {}

    def save_learned_field(self, question: str, answer: str):
        """Persist a user-supplied answer for a question so it never halts again."""
        if not question or answer is None:
            return
        norm_q = self._normalize_question(question)
        self.learned_cache[norm_q] = answer
        try:
            os.makedirs(os.path.dirname(self.learned_file) or ".", exist_ok=True)
            with open(self.learned_file, "w", encoding="utf-8") as f:
                json.dump(self.learned_cache, f, indent=2)
            logger.info(f"Learned mapping saved for question: '{norm_q}' -> '{answer}'")
        except Exception as e:
            logger.error(f"Failed to save learned field: {e}")

    def _normalize_question(self, question: str) -> str:
        q = question.lower().strip()
        q = re.sub(r'[*:\?]', '', q)
        return " ".join(q.split())

    def _match_choice(self, val: str, choices: List[str], category: str = "") -> Optional[str]:
        """Align a resolved value with the options in a select or radio button."""
        if not choices:
            return val

        v_low = str(val).lower().strip()

        # Exact match
        for c in choices:
            if c.lower().strip() == v_low:
                return c

        # Work Auth / Visa Status
        # Location Selection
        if category == "location":
            try:
                from form_filler import DeterministicEngine
                best = DeterministicEngine.pick_best_location_choice(choices)
                if best:
                    return best
            except Exception:
                pass
            for loc_kw in ["washington", "remote", "united states", "usa"]:
                for c in choices:
                    if loc_kw in c.lower() and not any(p in c.lower() for p in ["select", "choose", "--"]):
                        return c

        if category == "work_auth":
            # If candidate status requires sponsorship / OPT, prioritize matching OPT / Sponsorship options
            for auth_kw in ["f-1 opt", "opt / cpt", "opt", "stem opt", "sponsorship", "require sponsorship", "authorized with sponsorship"]:
                for c in choices:
                    if auth_kw in c.lower():
                        return c
            for c in choices:
                if any(y in c.lower() for y in ["yes", "authorized", "eligible"]):
                    return c

        # U.S. Person Status
        if category == "us_person":
            for c in choices:
                c_low = c.lower().strip()
                if "different country" in c_low or "none of the above" in c_low or "not a u.s." in c_low or "not a us" in c_low:
                    return c
            for c in choices:
                c_low = c.lower().strip()
                if c_low in ("no", "false", "neither", "other") or "not" in c_low:
                    return c

        # Boolean Negative / Auditor Independence
        if category == "boolean_negative":
            for c in choices:
                c_low = c.lower().strip()
                if any(n in c_low for n in ["never been employed", "never worked", "no - i have never", "no", "never", "false", "none"]):
                    return c
            for c in choices:
                if c.lower().strip() in ("no", "false", "none"):
                    return c

        # Boolean / Willingness
        if category == "boolean" or v_low in ("yes", "no", "true", "false"):
            if v_low in ("yes", "true"):
                for c in choices:
                    if any(y in c.lower() for y in ["yes", "authorized", "agree", "confirm", "accept", "eligible"]):
                        return c
                # Fallback to first non-negative
                for c in choices:
                    if not any(n in c.lower() for n in ["no", "never", "decline", "not"]):
                        return c
            elif v_low in ("no", "false"):
                for c in choices:
                    if any(n in c.lower() for n in ["no", "decline", "not", "none"]):
                        return c

        # Numeric / Years of Experience (e.g. 2 -> "1-3", "2+", "1 to 2", "2-3 years")
        if category == "numeric_years":
            num_match = re.search(r'\d+', v_low)
            target_num = int(num_match.group()) if num_match else 2

            # Try range match e.g. "1-3", "2-4", "2+"
            for c in choices:
                c_clean = c.lower().strip()
                # Single number match
                if c_clean == str(target_num):
                    return c
                # Range match e.g. "1 - 3", "1-3"
                rng = re.findall(r'\d+', c_clean)
                if len(rng) >= 2:
                    low_b, high_b = int(rng[0]), int(rng[1])
                    if low_b <= target_num <= high_b:
                        return c
                elif len(rng) == 1 and ("+" in c_clean or "more" in c_clean or "over" in c_clean):
                    if target_num >= int(rng[0]):
                        return c

        # Substring / word boundary (with critical collision guards)
        for c in choices:
            c_low = c.lower().strip()
            if v_low in ["asian", "south asian"] and "caucasian" in c_low:
                continue
            if v_low in ["male", "man"] and ("female" in c_low or "woman" in c_low):
                continue
            if v_low == "no" and not re.search(r"(?:^|\b)no(?:\b|$)", c_low):
                continue
            if v_low in c_low or c_low in v_low:
                return c

        return None

    def solve(self, question: str, choices: Optional[List[str]] = None) -> Optional[str]:
        """
        Attempt to solve a form question using:
        1. Previously learned user answers
        2. Exact regex matches in COMMON_FIELDS
        Returns the resolved string or None if unmapped.
        """
        if not question:
            return None

        norm_q = self._normalize_question(question)

        # 1. Check persistent learned answers
        if norm_q in self.learned_cache:
            raw_val = self.learned_cache[norm_q]
            res = self._match_choice(raw_val, choices) if choices else raw_val
            if res is not None:
                return res

        # Partial match in learned cache (require meaningful question length)
        for l_q, l_ans in self.learned_cache.items():
            if len(l_q) > 10 and (l_q in norm_q or norm_q in l_q):
                res = self._match_choice(l_ans, choices) if choices else l_ans
                if res is not None:
                    return res

        # 2. Match against COMMON_FIELDS registry (Exact regex patterns only)
        q_raw = question.lower()
        for field_key, meta in COMMON_FIELDS.items():
            for pat in meta["patterns"]:
                if re.search(pat, q_raw, re.IGNORECASE):
                    if field_key == "notice_period":
                        try:
                            from form_filler import resolve_start_date
                            val = resolve_start_date()
                        except Exception:
                            config_val = getattr(settings, meta["config_attr"], meta["default"])
                            val = config_val or meta["default"]
                    else:
                        config_val = getattr(settings, meta["config_attr"], meta["default"])
                        val = config_val or meta["default"]
                    res = self._match_choice(val, choices, category=meta["category"]) if choices else str(val)
                    if res is not None:
                        return res

        # 3. Fallback to Universal Question Solver (fuzzy registry & semantic profile map)
        try:
            from app.services.universal_registry import universal_solver
            ans, prov, stage = universal_solver.solve(question, choices)
            if ans is not None:
                return ans
        except Exception as ue:
            logger.debug(f"UniversalQuestionSolver fallback in common_fields: {ue}")

        return None


# Singleton resolver
common_field_resolver = CommonFieldResolver()

