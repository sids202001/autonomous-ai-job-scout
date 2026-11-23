"""
AutoApply - Universal Relevance Gate
Ensures applications strictly target verified candidate competencies derived
ONLY from candidate resume + portfolio + GitHub.

Candidate Profile Core:
- Domains: Software Engineering, Backend Engineering, Data Engineering, AWS Data Pipelines, AI Automation
- Target Roles: Software Engineer, Backend Engineer, Data Engineer, SDE, AI/ML Engineer, Full Stack Engineer
- Strict Prohibitions: Design, Marketing, Sales, Product Management, HR/Recruiting, Finance, Hardware/Civil
- Core Stack: Python, SQL, AWS, FastAPI, Docker, PostgreSQL, Redis, Kafka, ETL Pipelines, PyTorch, LangChain, TypeScript
"""

import re
import logging
from typing import Tuple, List, Dict, Any, Set

logger = logging.getLogger(__name__)

# Target role title patterns (eligible role families)
TARGET_ROLE_PATTERNS = [
    r"\bsoftware\s*(?:development\s*)?engineer\b",
    r"\bsoftware\s*developer\b",
    r"\bbackend\s*(?:software\s*)?(?:engineer|developer)\b",
    r"\bdata\s*(?:pipeline\s*)?engineer\b",
    r"\bdata\s*infrastructure\s*engineer\b",
    r"\betl\s*(?:developer|engineer)\b",
    r"\bsde\b",
    r"\bswe\b",
    r"\bai\s*(?:automation\s*)?(?:engineer|developer)\b",
    r"\bmachine\s*learning\s*engineer\b",
    r"\bml\s*engineer\b",
    r"\bllm\s*(?:application\s*)?(?:engineer|developer)\b",
    r"\bfull[\s\-]*stack\s*(?:software\s*)?(?:engineer|developer)\b",
    r"\bpython\s*(?:software\s*)?(?:developer|engineer)\b",
    r"\bcloud\s*(?:data\s*)?engineer\b",
    r"\bplatform\s*engineer\b",
]

# FORBIDDEN domains: NEVER apply, regardless of keyword overlap
FORBIDDEN_DOMAIN_PATTERNS = [
    # Design
    r"\bdesign(?:er)?\b",
    r"\bui[/\-]ux\b",
    r"\bux[/\-]ui\b",
    r"\bproduct\s*design(?:er)?\b",
    r"\bgraphic\s*design(?:er)?\b",
    r"\bvisual\s*design(?:er)?\b",
    r"\bart\s*director\b",
    # Marketing & Growth
    r"\bmarketing\b",
    r"\bgrowth\b",
    r"\bcontent\s*(?:writer|creator|marketing)\b",
    r"\bsocial\s*media\b",
    r"\bcopywriter\b",
    r"\bseo\b",
    r"\bbrand\s*manager\b",
    r"\bcommunications\b",
    # Sales & Business Development
    r"\bsales\b",
    r"\baccount\s*executive\b",
    r"\bbusiness\s*development\b",
    r"\bbdr\b",
    r"\bsdr\b",
    r"\baccount\s*manager\b",
    # Product & Project Management
    r"\bproduct\s*manager\b",
    r"\bproduct\s*management\b",
    r"\bprogram\s*manager\b",
    r"\bproject\s*manager\b",
    r"\bscrum\s*master\b",
    r"\bagile\s*coach\b",
    # HR & Recruiting
    r"\brecruiter\b",
    r"\brecruiting\b",
    r"\btalent\s*acquisition\b",
    r"\bhuman\s*resources\b",
    r"\bpeople\s*operations\b",
    # Finance, Accounting & Legal
    r"\baccountant\b",
    r"\baccounting\b",
    r"\bauditor\b",
    r"\bfinancial\s*analyst\b",
    r"\bcontroller\b",
    r"\blegal\b",
    r"\bparalegal\b",
    r"\bcounsel\b",
    # Non-software Engineering & Hardware
    r"\bmechanical\s*engineer\b",
    r"\belectrical\s*engineer\b",
    r"\bcivil\s*engineer\b",
    r"\bhardware\s*(?:engineer|technician|support|specialist)\b",
    r"\bmanufacturing\s*engineer\b",
    r"\bthermal\s*engineer\b",
    r"\bchemical\s*engineer\b",
    # Data Center, Facilities, Cable, Racking & Field Ops
    r"\bdata\s*center\s*(?:engineer|technician|specialist|ops|operations|lead|tech)\b",
    r"\bdatacenter\s*(?:engineer|technician|specialist|ops|operations|lead|tech)\b",
    r"\bdc\s*technician\b",
    r"\bcable\s*(?:technician|engineer|installer)\b",
    r"\bcabling\b",
    r"\brack\s*(?:and\s*stack|engineer|technician)\b",
    r"\bfacilities\s*(?:engineer|technician|manager|operations)\b",
    r"\bfield\s*(?:engineer|technician|operations|service|ops)\b",
    r"\bit\s*support\b",
    r"\bdesktop\s*support\b",
    r"\bnoc\s*technician\b",
    r"\btelecommunications\s*technician\b",
    r"\boptical\s*technician\b",
    # Support, Administrative & Assistant
    r"\bcustomer\s*(?:support|service|success)\b",
    r"\bhelp\s*desk\b",
    r"\boffice\s*manager\b",
    r"\bassistant\b",
    r"\bexecutive\s*assistant\b",
    r"\badministrative\b",
    # Analyst (verification/background/compliance/revenue/business/operations)
    r"\banalyst\b",
    r"\bverification\b",
    r"\bcompliance\b",
]

# Verified tech skills from Resume + Portfolio + GitHub
CANDIDATE_SKILL_STACK: Set[str] = {
    # Core Languages & Runtime
    "python", "sql", "typescript", "javascript", "bash", "java", "c++",
    # Backend Frameworks & Architecture
    "fastapi", "flask", "django", "rest", "restful", "rest api", "graphql", "microservices",
    # Databases & Storage
    "postgresql", "postgres", "mysql", "redis", "mongodb", "dynamodb", "redshift", "sqlite",
    # Cloud & DevOps
    "aws", "lambda", "ecs", "s3", "docker", "kubernetes", "k8s", "ci/cd", "terraform", "cloudwatch",
    # Data & Pipelines
    "kafka", "etl", "data pipeline", "data pipelines", "pandas", "numpy", "spark", "airflow",
    # AI & Automation
    "llm", "langchain", "openai", "pytorch", "machine learning", "prompt engineering", "genai", "generative ai",
}


class RelevanceGate:
    """
    Gate validating job title and skills match candidate profile before application.
    """

    MIN_RELEVANCE_THRESHOLD = 0.60

    # MATCH BAR: posting passes iff ≥2 distinct candidate skills appear
    # in combined title+description (case-insensitive, alias-aware).
    MATCH_BAR_MIN_SKILLS = 2

    @classmethod
    def evaluate(cls, title: str, description: str) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Evaluate relevance of posting against candidate profile.

        Returns:
            (is_relevant: bool, decision_reason: str, metadata: dict)
        """
        title_str = (title or "").strip()
        desc_str = (description or "").strip()
        combined_text = f"{title_str} {desc_str}".lower()

        metadata: Dict[str, Any] = {
            "title": title_str,
            "title_score": 0.0,
            "stack_score": 0.0,
            "relevance_score": 0.0,
            "matched_skills": [],
            "threshold": cls.MIN_RELEVANCE_THRESHOLD,
        }

        # 1. HARD PROHIBITION: Check if title is in forbidden non-engineering domains
        for pat in FORBIDDEN_DOMAIN_PATTERNS:
            if re.search(pat, title_str, re.IGNORECASE):
                reason = f"WRONG_FUNCTION: FORBIDDEN_DOMAIN: Title '{title_str}' falls into non-target domain (pattern: '{pat}'). Never applying outside profile."
                logger.info(reason)
                metadata["relevance_score"] = 0.0
                return False, reason, metadata

        # 2. TITLE MATCH: Check if title matches target engineering role families
        is_target_role = any(re.search(pat, title_str, re.IGNORECASE) for pat in TARGET_ROLE_PATTERNS)
        is_generic_eng = bool(re.search(r"\b(?:engineer|developer)\b", title_str, re.IGNORECASE))

        if is_target_role:
            title_score = 1.0
        elif is_generic_eng:
            title_score = 0.6
        else:
            reason = f"WRONG_FUNCTION: NON_TARGET_ROLE: Title '{title_str}' is outside candidate target role families (Software/Data/AI Engineering)."
            logger.info(reason)
            metadata["relevance_score"] = 0.0
            return False, reason, metadata

        metadata["title_score"] = title_score

        # 3. SKILL OVERLAP: Count verified candidate tech stack skills mentioned in description
        matched_skills = []
        for skill in CANDIDATE_SKILL_STACK:
            # Word boundary regex search
            pattern = r"\b" + re.escape(skill) + r"\b"
            if re.search(pattern, combined_text, re.IGNORECASE):
                matched_skills.append(skill)

        metadata["matched_skills"] = matched_skills
        # 3+ matching skills gives full stack score of 1.0; 2 skills gives 0.67; 1 skill gives 0.33
        stack_score = min(1.0, len(matched_skills) / 3.0)
        metadata["stack_score"] = round(stack_score, 2)

        # 3b. MATCH BAR: posting passes iff ≥2 distinct candidate skills appear
        if len(matched_skills) < cls.MATCH_BAR_MIN_SKILLS:
            reason = (
                f"MATCH_BAR_FAIL: Only {len(matched_skills)} candidate skill(s) matched "
                f"(require ≥{cls.MATCH_BAR_MIN_SKILLS}): [{', '.join(matched_skills or ['none'])}] — "
                f"Title: '{title_str}'"
            )
            logger.info(reason)
            metadata["relevance_score"] = 0.0
            return False, reason, metadata

        # 4. COMBINED RELEVANCE SCORE
        # 50% title fidelity + 50% stack overlap
        relevance_score = round(0.5 * title_score + 0.5 * stack_score, 2)
        metadata["relevance_score"] = relevance_score

        # 4b. TITLE MATCH GUARANTEE: if toggle is ON and title is a direct
        # target-role match, bypass the threshold check entirely.
        if is_target_role and relevance_score < cls.MIN_RELEVANCE_THRESHOLD:
            try:
                from config import settings
                title_guarantee = settings.TITLE_MATCH_GUARANTEES_RELEVANCE
            except Exception:
                title_guarantee = False

            if title_guarantee:
                reason = (
                    f"TITLE_MATCH_GUARANTEE: Score {relevance_score:.2f} below threshold {cls.MIN_RELEVANCE_THRESHOLD}, "
                    f"but title '{title_str}' is a direct target-role match → bypassed under TITLE_MATCH_GUARANTEES_RELEVANCE toggle. "
                    f"(Matched Stack ({len(matched_skills)}): {', '.join(matched_skills[:5]) or 'none'})"
                )
                logger.info(reason)
                return True, reason, metadata

        if relevance_score >= cls.MIN_RELEVANCE_THRESHOLD:
            reason = (
                f"RELEVANCE_PASSED: Score {relevance_score:.2f} >= threshold {cls.MIN_RELEVANCE_THRESHOLD} "
                f"(Title: '{title_str}', Matched Stack ({len(matched_skills)}): {', '.join(matched_skills[:5])})"
            )
            logger.info(reason)
            return True, reason, metadata
        else:
            reason = (
                f"LOW_RELEVANCE: Score {relevance_score:.2f} below threshold {cls.MIN_RELEVANCE_THRESHOLD} "
                f"(Insufficient stack overlap: {len(matched_skills)} skills matched [{', '.join(matched_skills)}])"
            )
            logger.info(reason)
            return False, reason, metadata


# Singleton helper
relevance_gate = RelevanceGate()
