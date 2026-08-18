"""
AutoApply - Enhanced Job Matcher Service
Scores jobs with TF-IDF similarity, H-1B visa hard filtering,
experience level filtering, and detailed match reasons.
"""
import logging
import re
import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import Job, Profile, Skill, JobFilter

logger = logging.getLogger(__name__)

# H-1B / Visa sponsorship rejection patterns
NO_SPONSORSHIP_PATTERNS = [
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

# Senior/advanced title patterns to reject
SENIOR_TITLE_PATTERNS = [
    r'\bsenior\b', r'\bsr\.?\b', r'\blead\b', r'\bstaff\b',
    r'\bprincipal\b', r'\bmanager\b', r'\bdirector\b', r'\barchitect\b',
    r'\bhead\s+of\b', r'\bvp\b', r'\biii\b', r'\biv\b',
]

# Forbidden domain keywords in titles
FORBIDDEN_DOMAINS = [
    'mechanical', 'thermal', 'collision', 'physics', 'manufacturing',
    'hardware', 'equipment', 'remanufacturing', 'materials', 'process',
    'civil', 'electrical', 'chemical',
]


class JobMatcher:
    """
    Enhanced job scoring engine with hard filters and TF-IDF matching.

    Scoring breakdown (100 points total):
    - TF-IDF / text similarity: 30 points
    - Skill overlap: 25 points
    - Experience level match: 15 points
    - Location match: 15 points
    - Salary range overlap: 10 points
    - Remote preference: 5 points

    Hard Filters (auto-reject, score = 0):
    - No visa sponsorship available
    - Senior/Lead/Staff/Principal titles
    - Requires 4+ years experience
    - Forbidden engineering domains
    """

    # ── Hard Filters ─────────────────────────────────────────────────────

    def _check_visa_sponsorship(self, job: Job) -> tuple[bool, str]:
        """Check if job allows H-1B/visa sponsorship. Returns (passes, reason)."""
        desc = (job.description or "").lower()
        for pattern in NO_SPONSORSHIP_PATTERNS:
            if re.search(pattern, desc):
                return False, f"No visa sponsorship (matched: {pattern[:40]})"
        return True, ""

    def _check_experience_level(self, job: Job) -> tuple[bool, str]:
        """Check if job is appropriate for <=3 years experience. Returns (passes, reason)."""
        title_lower = (job.title or "").lower()
        desc_lower = (job.description or "").lower()

        # Check senior title patterns
        for pattern in SENIOR_TITLE_PATTERNS:
            if re.search(pattern, title_lower):
                return False, f"Senior-level title (matched: {pattern})"

        # Check forbidden domains
        for domain in FORBIDDEN_DOMAINS:
            if domain in title_lower:
                return False, f"Forbidden domain: {domain}"

        # Check explicit year requirements > 3
        exp_patterns = [
            r'(\d+)\+?\s*(?:years?|yrs?)\s*(?:of)?\s*experience',
            r'minimum\s+of\s+(\d+)\s+years?',
            r'(\d+)\s+years?\s+of\s+(?:relevant|professional|work)?\s*experience',
        ]
        for pattern in exp_patterns:
            match = re.search(pattern, desc_lower)
            if match:
                try:
                    yrs = int(match.group(1))
                    if yrs >= 4:
                        return False, f"Requires {yrs}+ years of experience"
                except (ValueError, IndexError):
                    pass

        # Check text-based year requirements
        text_years = {
            'four': 4, 'five': 5, 'six': 6, 'seven': 7,
            'eight': 8, 'nine': 9, 'ten': 10
        }
        for word, val in text_years.items():
            if re.search(rf'{word}\s+years?', desc_lower):
                return False, f"Requires {val}+ years of experience"

        return True, ""

    # ── Scoring Components ───────────────────────────────────────────────

    def _tfidf_score(self, job: Job, resume_text: str) -> float:
        """Score based on TF-IDF cosine similarity between job and resume (0-30)."""
        if not resume_text:
            return 15.0  # neutral

        job_text = f"{job.title} {job.description or ''} {job.requirements or ''}"
        if not job_text.strip():
            return 15.0

        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity

            vectorizer = TfidfVectorizer(stop_words='english', max_features=5000)
            tfidf_matrix = vectorizer.fit_transform([resume_text, job_text])
            similarity = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])[0][0]
            return round(min(similarity * 30 * 2, 30.0), 1)  # Scale up since cosine is usually low
        except ImportError:
            # Fallback: simple keyword overlap
            resume_words = set(resume_text.lower().split())
            job_words = set(job_text.lower().split())
            if not job_words:
                return 15.0
            overlap = len(resume_words & job_words) / len(job_words)
            return round(min(overlap * 30 * 3, 30.0), 1)
        except Exception as e:
            logger.warning(f"TF-IDF scoring failed: {e}")
            return 15.0

    def _skill_score(self, job: Job, skills: list[str], filter_keywords: list[str]) -> float:
        """Score based on skill/keyword match (0-25)."""
        if not skills and not filter_keywords:
            return 12.5

        all_keywords = set(kw.lower() for kw in skills + filter_keywords)
        if not all_keywords:
            return 12.5

        text = f"{job.title} {job.description or ''} {job.requirements or ''}".lower()
        matches = sum(1 for kw in all_keywords if kw in text)
        ratio = matches / len(all_keywords) if all_keywords else 0
        return round(min(ratio * 25, 25), 1)

    def _salary_score(self, job: Job, profile: Profile) -> float:
        """Score based on salary range overlap (0-10)."""
        if not job.salary_min and not job.salary_max:
            return 5.0
        if not profile.desired_salary_min and not profile.desired_salary_max:
            return 5.0

        job_min = job.salary_min or 0
        job_max = job.salary_max or job_min
        prof_min = profile.desired_salary_min or 0
        prof_max = profile.desired_salary_max or prof_min

        if job_max < prof_min:
            gap = prof_min - job_max
            penalty = min(gap / prof_min, 1.0) if prof_min else 1.0
            return round(max(0, 10 * (1 - penalty)), 1)
        elif job_min > prof_max and prof_max > 0:
            return 10.0
        else:
            overlap = min(job_max, prof_max) - max(job_min, prof_min)
            total_range = max(job_max, prof_max) - min(job_min, prof_min)
            ratio = overlap / total_range if total_range > 0 else 1
            return round(ratio * 10, 1)

    def _location_score(self, job: Job, filters: list[JobFilter]) -> float:
        """Score based on location match (0-15)."""
        if not filters:
            return 7.5
        job_location = (job.location or "").lower()
        for f in filters:
            filter_loc = (f.location or "").lower()
            if not filter_loc or filter_loc == "any":
                return 15.0
            if filter_loc in job_location or job_location in filter_loc:
                return 15.0
            if "remote" in job_location:
                return 12.0
        return 3.0

    def _experience_score(self, job: Job, profile: Profile) -> float:
        """Score based on experience level match (0-15)."""
        if not profile.years_experience:
            return 7.5
        title = (job.title or "").lower()
        years = profile.years_experience
        if any(word in title for word in ["junior", "associate", "entry", "new grad"]):
            return 15.0 if years <= 3 else 5.0
        elif any(word in title for word in ["senior", "staff", "lead", "principal"]):
            return 3.0 if years < 3 else 15.0
        else:
            return 12.0 if years <= 5 else 10.0

    def _remote_score(self, job: Job, filters: list[JobFilter]) -> float:
        """Score based on remote preference match (0-5)."""
        if not filters:
            return 2.5
        job_remote = (job.remote_type or "").lower()
        for f in filters:
            pref = (f.remote_preference or "any").lower()
            if pref == "any":
                return 5.0
            if pref in job_remote or job_remote in pref:
                return 5.0
            if pref == "remote" and "remote" in (job.location or "").lower():
                return 5.0
        return 1.0

    # ── Match Reasons ────────────────────────────────────────────────────

    def _generate_match_reasons(self, job, skills_matched, tfidf, skill, salary, location, experience, remote, visa_ok, exp_ok) -> list[str]:
        """Generate human-readable match reasons."""
        reasons = []
        if not visa_ok[0]:
            reasons.append(f"❌ {visa_ok[1]}")
        if not exp_ok[0]:
            reasons.append(f"❌ {exp_ok[1]}")
        if tfidf >= 20:
            reasons.append(f"✅ Strong resume match ({tfidf:.0f}/30)")
        elif tfidf >= 10:
            reasons.append(f"🟡 Moderate resume match ({tfidf:.0f}/30)")
        if skill >= 15:
            reasons.append(f"✅ Good skill overlap ({skill:.0f}/25)")
        if salary >= 8:
            reasons.append(f"✅ Salary in range")
        if location >= 12:
            reasons.append(f"✅ Location match")
        if remote >= 4:
            reasons.append(f"✅ Remote preference match")
        return reasons

    # ── Main Scoring ─────────────────────────────────────────────────────

    def score_job(
        self, job: Job, profile: Profile,
        skills: list[str], filter_keywords: list[str],
        filters: list[JobFilter], resume_text: str = ""
    ) -> tuple[float, list[str]]:
        """Calculate overall match score (0-100) with hard filters and match reasons."""
        # Hard filters first
        visa_ok = self._check_visa_sponsorship(job)
        exp_ok = self._check_experience_level(job)

        if not visa_ok[0] or not exp_ok[0]:
            reasons = self._generate_match_reasons(job, [], 0, 0, 0, 0, 0, 0, visa_ok, exp_ok)
            return 0.0, reasons

        # Calculate component scores
        tfidf = self._tfidf_score(job, resume_text)
        skill = self._skill_score(job, skills, filter_keywords)
        sal = self._salary_score(job, profile)
        loc = self._location_score(job, filters)
        exp = self._experience_score(job, profile)
        rem = self._remote_score(job, filters)

        total = round(tfidf + skill + sal + loc + exp + rem, 1)
        total = min(total, 100.0)

        reasons = self._generate_match_reasons(job, skills, tfidf, skill, sal, loc, exp, rem, visa_ok, exp_ok)
        return total, reasons

    async def score_all_jobs(self, db: AsyncSession, profile_id: int) -> int:
        """Score all jobs in the database for a given profile."""
        profile = (await db.execute(
            select(Profile).where(Profile.id == profile_id)
        )).scalar_one_or_none()
        if not profile:
            return 0

        skills_result = await db.execute(
            select(Skill).where(Skill.profile_id == profile_id)
        )
        skills = [s.skill_name for s in skills_result.scalars().all()]

        filters_result = await db.execute(
            select(JobFilter).where(
                JobFilter.profile_id == profile_id,
                JobFilter.is_active == True,
            )
        )
        filters = filters_result.scalars().all()
        filter_keywords = []
        for f in filters:
            if f.role_keywords:
                filter_keywords.extend([kw.strip() for kw in f.role_keywords.split(",")])

        # Get resume text for TF-IDF
        resume_text = ""
        if profile.resume_path:
            try:
                from app.services.resume_parser import ResumeParser
                parser = ResumeParser()
                resume_text = parser.get_full_text(profile.resume_path)
            except Exception as e:
                logger.warning(f"Could not load resume for TF-IDF: {e}")

        # Score all visible jobs
        jobs_result = await db.execute(select(Job).where(Job.is_hidden == False))
        jobs = jobs_result.scalars().all()

        scored = 0
        for job in jobs:
            score, reasons = self.score_job(job, profile, skills, filter_keywords, filters, resume_text)
            job.match_score = score
            # Store match reasons in requirements field as JSON
            try:
                job.requirements = json.dumps(reasons)
            except Exception:
                pass
            # Auto-hide jobs that fail hard filters
            if score == 0 and reasons and any('❌' in r for r in reasons):
                job.is_hidden = True
            scored += 1

        await db.commit()
        logger.info(f"Scored {scored} jobs for profile {profile_id}")
        return scored
