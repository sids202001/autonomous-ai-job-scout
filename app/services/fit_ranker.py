"""
AutoApply - Candidate Fit Ranker
Implements:
Fit ranking: skill-fit % (resume + portfolio + GitHub) × recency × H-1B signal.
Queue strictly rank-ordered; log rank per launch.
"""
import os
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Set, Tuple, Optional, Any, Union

logger = logging.getLogger(__name__)

# Portfolio and GitHub technical skills (extracted from candidate's live portfolio & GitHub)
CANDIDATE_PORTFOLIO_SKILLS = [
    "power bi", "tableau", "dax", "power query", "excel", "snowflake",
    "scikit-learn", "random forest", "linear regression", "d3.js",
    "node.js", "express.js", "mongodb"
]

CANDIDATE_GITHUB_SKILLS = [
    "playwright", "fastapi", "asyncio", "sqlite", "telegram bot api",
    "pypdf2", "beautifulsoup", "web scraping", "python", "docker", "git"
]

# Standard technical resume skills vocabulary
RESUME_TECHNICAL_SKILLS = [
    "python", "sql", "javascript", "typescript", "java", "c++", "golang",
    "postgresql", "postgres", "mongodb", "redshift", "snowflake", "sqlite", "mysql", "dynamodb", "redis",
    "pandas", "numpy", "matplotlib", "plotly", "scipy", "aws", "s3", "ec2", "step functions",
    "lambda", "cloudwatch", "iam", "api gateway", "docker", "kubernetes", "linux", "unix", "ci/cd",
    "etl", "data pipeline", "pipeline", "workflow orchestration", "data engineering",
    "rest api", "restful", "apis", "api", "backend", "fullstack", "full stack",
    "machine learning", "deep learning", "nlp", "airflow", "spark", "kafka", "dbt", "bigquery"
]

NO_SPONSOR_KEYWORDS = [
    r'not\s+offering\s+(?:visa\s+)?sponsorship',
    r'unable\s+to\s+(?:offer|provide|sponsor)\s+(?:visa\s+)?sponsorship',
    r'will\s+not\s+(?:sponsor|provide|offer)\s+(?:visa\s+)?sponsorship',
    r'no\s+sponsorship\s+available',
    r'does\s+not\s+sponsor',
    r'cannot\s+sponsor',
    r'no\s+h-?1b',
    r'must\s+be\s+a\s+u\.?s\.?\s+citizen',
    r'us\s+citizen\s+only',
    r'authorization\s+to\s+work.*without\s+sponsorship',
    r'without\s+requiring\s+(?:visa\s+)?sponsorship',
    r'without\s+sponsorship\s+now\s+or\s+in\s+the\s+future',
    r'security\s+clearance\s+required',
    r'active\s+clearance\s+required',
    r'no\s+opt', r'no\s+cpt',
    r'not\s+open\s+to\s+opt',
]

POSITIVE_SPONSOR_KEYWORDS = [
    r'visa\s+sponsorship\s+(?:is\s+)?available',
    r'will\s+sponsor\s+(?:visa|h-?1b)',
    r'sponsorship\s+is\s+available',
    r'open\s+to\s+sponsoring',
    r'sponsors\s+h-?1b',
    r'provides?\s+(?:visa\s+)?sponsorship',
    r'support\s+(?:for\s+)?visa\s+sponsorship',
]


class FitRanker:
    """
    Computes deterministic fit ranking:
    rank_score = skill_fit_pct * recency_weight * h1b_signal
    """
    def __init__(self, resume_path: Optional[str] = None):
        self.resume_path = resume_path or os.path.expanduser("~/Desktop/current_resume.pdf")
        self._cached_candidate_skills: Optional[Set[str]] = None

    def get_candidate_skills(self) -> Set[str]:
        """Combine skills from Resume + Portfolio + GitHub."""
        if self._cached_candidate_skills:
            return self._cached_candidate_skills

        skills = set()
        # 1. Resume skills
        if os.path.exists(self.resume_path):
            try:
                from PyPDF2 import PdfReader
                reader = PdfReader(self.resume_path)
                full_text = " ".join([page.extract_text() for page in reader.pages]).lower()
                for s in RESUME_TECHNICAL_SKILLS:
                    if re.search(rf'\b{re.escape(s)}\b', full_text):
                        skills.add(s)
            except Exception as e:
                logger.debug(f"Resume skill extraction error: {e}")

        # Fallback if resume text had no extracted skills
        if not skills:
            skills.update(RESUME_TECHNICAL_SKILLS[:15])

        # 2. Portfolio skills
        skills.update(CANDIDATE_PORTFOLIO_SKILLS)

        # 3. GitHub skills
        skills.update(CANDIDATE_GITHUB_SKILLS)

        self._cached_candidate_skills = {s.lower() for s in skills}
        return self._cached_candidate_skills

    def calculate_skill_fit(self, title: str, description: str) -> Tuple[float, List[str]]:
        """
        Calculate skill-fit percentage across (resume + portfolio + GitHub).
        Returns (skill_fit_pct, matched_skills).
        """
        candidate_skills = self.get_candidate_skills()
        text = f"{title or ''} {description or ''}".lower()

        matched = []
        for s in candidate_skills:
            if ' ' in s or '-' in s:
                if s in text:
                    matched.append(s)
            else:
                if re.search(rf'\b{re.escape(s)}\b', text):
                    matched.append(s)

        # Score mapping:
        # >= 5 matched skills: 1.0 (100%)
        # 4 matched skills: 0.85
        # 3 matched skills: 0.70
        # 2 matched skills: 0.55
        # 1 matched skill: 0.40
        # 0 matched skills: 0.0
        n = len(matched)
        if n >= 5:
            pct = 1.0
        elif n == 4:
            pct = 0.85
        elif n == 3:
            pct = 0.70
        elif n == 2:
            pct = 0.55
        elif n == 1:
            pct = 0.40
        else:
            pct = 0.0

        return pct, sorted(matched)

    @staticmethod
    def calculate_recency(created_at: Optional[Union[str, datetime]] = None) -> float:
        """
        Recency weighting based on posting age:
        < 24h: 1.0
        1-3d: 0.90
        4-7d: 0.75
        8-14d: 0.55
        >14d or unknown: 0.40
        """
        if not created_at:
            return 0.50  # Neutral default for unstated date

        dt = None
        if isinstance(created_at, datetime):
            dt = created_at
        elif isinstance(created_at, str):
            try:
                # Handle ISO formats
                clean_str = created_at.replace("Z", "+00:00")
                dt = datetime.fromisoformat(clean_str)
            except Exception:
                pass

        if not dt:
            return 0.50

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
        age_days = max(0.0, age_seconds / 86400.0)

        if age_days <= 1.0:
            return 1.0
        elif age_days <= 3.0:
            return 0.90
        elif age_days <= 7.0:
            return 0.75
        elif age_days <= 14.0:
            return 0.55
        else:
            return 0.40

    @staticmethod
    def calculate_h1b_signal(description: str) -> float:
        """
        H-1B sponsorship signal:
        - Explicit disqualification (no sponsorship / citizen only): 0.0
        - Explicit positive sponsorship available: 1.0
        - Neutral (no negative disclaimer): 0.85
        """
        if not description:
            return 0.85

        desc_lower = description.lower()

        # Check negative patterns
        for pattern in NO_SPONSOR_KEYWORDS:
            if re.search(pattern, desc_lower):
                return 0.0

        # Check positive patterns
        for pattern in POSITIVE_SPONSOR_KEYWORDS:
            if re.search(pattern, desc_lower):
                return 1.0

        return 0.85

    def score_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """
        Computes fit score for job dict:
        rank_score = skill_fit_pct * recency_weight * h1b_signal
        """
        title = job.get("title", "")
        desc = job.get("description", "")
        created_at = job.get("created_at") or job.get("updated_at")

        skill_fit_pct, matched_skills = self.calculate_skill_fit(title, desc)
        recency_weight = self.calculate_recency(created_at)
        h1b_signal = self.calculate_h1b_signal(desc)

        rank_score = round(skill_fit_pct * recency_weight * h1b_signal, 4)

        result = dict(job)
        result["skill_fit_pct"] = skill_fit_pct
        result["matched_skills"] = matched_skills
        result["recency_weight"] = recency_weight
        result["h1b_signal"] = h1b_signal
        result["fit_score"] = rank_score
        return result

    def rank_and_order_queue(self, jobs: List[Dict[str, Any]], filter_zero: bool = False) -> List[Dict[str, Any]]:
        """
        Takes list of jobs, scores each, and returns strictly rank-ordered queue descending by fit_score.
        Assigns 'rank' field 1..N.
        """
        scored_jobs = [self.score_job(j) for j in jobs]
        if filter_zero:
            scored_jobs = [j for j in scored_jobs if j["fit_score"] > 0.0]
        # Strictly rank-ordered descending
        scored_jobs.sort(key=lambda j: j["fit_score"], reverse=True)

        for idx, j in enumerate(scored_jobs, start=1):
            j["rank"] = idx

        return scored_jobs

    @staticmethod
    def log_launch(job: Dict[str, Any], launch_id: Optional[str] = None):
        """Log rank per launch."""
        rank = job.get("rank", "unranked")
        comp = job.get("company", "Unknown")
        title = job.get("title", "Role")
        score = job.get("fit_score", 0.0)
        lid = f"[{launch_id}] " if launch_id else ""
        logger.info(f"{lid}Launching role rank #{rank} ({comp} - {title}, fit_score={score:.4f}, skill_fit={job.get('skill_fit_pct', 0.0):.2f}, recency={job.get('recency_weight', 0.0):.2f}, h1b={job.get('h1b_signal', 0.0):.2f})")


fit_ranker = FitRanker()
