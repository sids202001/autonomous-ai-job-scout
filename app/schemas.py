"""
AutoApply - Pydantic Schemas
Request/response validation models.
"""
from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# ── Profile ──────────────────────────────────────────────────────────────────
class ProfileCreate(BaseModel):
    full_name: str = "Siddhesh Santosh Sawant"
    first_name: Optional[str] = "Siddhesh Santosh"
    last_name: Optional[str] = "Sawant"
    email: str = "siddhesh202001@gmail.com"
    phone: Optional[str] = "+1 445-208-2299"
    location: Optional[str] = "Washington, DC"
    country: Optional[str] = "United States"
    linkedin_url: Optional[str] = "https://www.linkedin.com/in/sss20/"
    github_url: Optional[str] = "https://github.com/sids202001"
    portfolio_url: Optional[str] = "https://siddhesh202001-portfolio.netlify.app"
    summary: Optional[str] = None
    work_authorization: Optional[str] = "US Citizen / Authorized"
    sponsorship_required: Optional[bool] = True
    authorized_us: Optional[bool] = True
    desired_salary_min: Optional[int] = 80000
    desired_salary_max: Optional[int] = 160000
    years_experience: Optional[int] = 2
    recent_degree: Optional[str] = "Masters"
    school: Optional[str] = "George Mason University"
    current_company: Optional[str] = "Talviro INC"
    current_title: Optional[str] = "Software Developer"
    gender: Optional[str] = "Male"
    hispanic_latino: Optional[str] = "No"
    race_ethnicity: Optional[str] = "Asian"
    disability_status: Optional[str] = "No"
    veteran_status: Optional[str] = "Non-Veteran"


class ProfileUpdate(ProfileCreate):
    full_name: Optional[str] = None
    email: Optional[str] = None


class ProfileResponse(ProfileCreate):
    id: int
    resume_path: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Experience ───────────────────────────────────────────────────────────────
class ExperienceCreate(BaseModel):
    company: str
    title: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    is_current: bool = False
    description: Optional[str] = None
    location: Optional[str] = None


class ExperienceResponse(ExperienceCreate):
    id: int
    profile_id: int

    class Config:
        from_attributes = True


# ── Education ────────────────────────────────────────────────────────────────
class EducationCreate(BaseModel):
    school: str
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    gpa: Optional[float] = None


class EducationResponse(EducationCreate):
    id: int
    profile_id: int

    class Config:
        from_attributes = True


# ── Skill ────────────────────────────────────────────────────────────────────
class SkillCreate(BaseModel):
    skill_name: str
    proficiency_level: Optional[str] = "Intermediate"


class SkillResponse(SkillCreate):
    id: int
    profile_id: int

    class Config:
        from_attributes = True


# ── Job Filter ───────────────────────────────────────────────────────────────
class JobFilterCreate(BaseModel):
    name: str = "Default Filter"
    role_keywords: Optional[str] = None
    location: Optional[str] = None
    remote_preference: str = "Any"
    min_salary: Optional[int] = None
    max_salary: Optional[int] = None
    experience_level: Optional[str] = None
    excluded_companies: Optional[str] = None
    job_type: str = "Full-time"
    is_active: bool = True


class JobFilterUpdate(JobFilterCreate):
    name: Optional[str] = None


class JobFilterResponse(JobFilterCreate):
    id: int
    profile_id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Job ──────────────────────────────────────────────────────────────────────
class JobResponse(BaseModel):
    id: int
    title: str
    company: str
    location: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    job_type: Optional[str] = None
    remote_type: Optional[str] = None
    description: Optional[str] = None
    requirements: Optional[str] = None
    url: str
    source: Optional[str] = None
    match_score: float = 0.0
    posted_date: Optional[str] = None
    scraped_at: Optional[datetime] = None
    is_hidden: bool = False

    class Config:
        from_attributes = True


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int
    page: int
    per_page: int


# ── Application ──────────────────────────────────────────────────────────────
class ApplicationCreate(BaseModel):
    job_id: int
    profile_id: int = 1
    notes: Optional[str] = None


class ApplicationUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    follow_up_date: Optional[str] = None
    cover_letter: Optional[str] = None


class ApplicationResponse(BaseModel):
    id: int
    job_id: int
    profile_id: int
    status: str
    applied_at: Optional[datetime] = None
    notes: Optional[str] = None
    follow_up_date: Optional[str] = None
    cover_letter: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    job: Optional[JobResponse] = None

    class Config:
        from_attributes = True


# ── Dashboard Stats ──────────────────────────────────────────────────────────
class DashboardStats(BaseModel):
    total_jobs: int = 0
    total_applied: int = 0
    total_interviews: int = 0
    total_saved: int = 0
    total_offers: int = 0
    recent_jobs: list[JobResponse] = []
