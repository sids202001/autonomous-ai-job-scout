"""
AutoApply - Profile Router
CRUD operations for user profile, experience, education, and skills.
"""
import os
import logging
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import Profile, Experience, Education, Skill, JobFilter
from app.schemas import (
    ProfileCreate, ProfileUpdate, ProfileResponse,
    ExperienceCreate, ExperienceResponse,
    EducationCreate, EducationResponse,
    SkillCreate, SkillResponse,
)
from app.services.resume_parser import ResumeParser
from config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Profile CRUD ─────────────────────────────────────────────────────────────

@router.post("/", response_model=ProfileResponse)
async def create_profile(data: ProfileCreate, db: AsyncSession = Depends(get_db)):
    """Create a new user profile."""
    profile = Profile(**data.model_dump(exclude_none=True))
    db.add(profile)
    await db.flush()
    await db.refresh(profile)
    logger.info(f"Created profile: {profile.full_name}")
    return profile


@router.get("/", response_model=ProfileResponse)
async def get_profile(db: AsyncSession = Depends(get_db)):
    """Get the current user profile."""
    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="No profile found. Create one first.")
    return profile


@router.put("/", response_model=ProfileResponse)
async def update_profile(data: ProfileUpdate, db: AsyncSession = Depends(get_db)):
    """Update the current user profile."""
    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="No profile found.")
    for key, value in data.model_dump(exclude_none=True).items():
        setattr(profile, key, value)
    await db.flush()
    await db.refresh(profile)
    return profile


@router.post("/resume")
async def upload_resume(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    """Upload a resume PDF or DOCX file with security checks."""
    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Create a profile first.")

    # Validate file extension
    allowed_extensions = {".pdf", ".docx"}
    file_ext = os.path.splitext(file.filename)[1].lower()
    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"Invalid file type '{file_ext}'. Only PDF and DOCX allowed.")

    # Validate file size (max 10MB)
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Maximum 10MB allowed.")

    # Sanitize filename
    safe_filename = os.path.basename(file.filename)
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(settings.UPLOAD_DIR, safe_filename)

    with open(file_path, "wb") as f:
        f.write(content)

    profile.resume_path = file_path
    await db.flush()
    return {"message": "Resume uploaded successfully", "path": file_path}



@router.post("/resume-path")
async def set_resume_path(payload: dict, db: AsyncSession = Depends(get_db)):
    """Set local resume file path, parse text, extract skills, and generate job filter."""
    path = payload.get("path")
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=400, detail=f"File not found at path: {path}")

    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        profile = Profile(full_name="User", email="user@example.com")
        db.add(profile)
        await db.flush()
        await db.refresh(profile)

    profile.resume_path = path

    # Parse resume text & extract structured details
    parser = ResumeParser()
    parse_res = parser.parse_pdf(path)
    extracted_skills = []
    if parse_res["success"]:
        ext = parser.extract_structured_data(parse_res["raw_text"])
        extracted_skills = ext["skills"]
        if ext["years_experience"]:
            profile.years_experience = ext["years_experience"]
        if ext["summary_snippet"] and not profile.summary:
            profile.summary = ext["summary_snippet"]

        # Save extracted skills
        existing_skills_res = await db.execute(select(Skill.skill_name).where(Skill.profile_id == profile.id))
        existing_skills = set(s[0].lower() for s in existing_skills_res.all())
        for skill_name in ext["skills"]:
            if skill_name.lower() not in existing_skills:
                db.add(Skill(profile_id=profile.id, skill_name=skill_name, proficiency_level="Intermediate"))
                existing_skills.add(skill_name.lower())

        # Create or update active JobFilter based on resume role_keywords & skills
        role_kws = ", ".join(ext["role_keywords"] + ext["skills"][:6])
        filter_res = await db.execute(
            select(JobFilter).where(JobFilter.profile_id == profile.id, JobFilter.name == "Resume Auto-Filter")
        )
        job_filter = filter_res.scalar_one_or_none()
        if not job_filter:
            job_filter = JobFilter(
                profile_id=profile.id,
                name="Resume Auto-Filter",
                role_keywords=role_kws,
                location=profile.location or "Remote",
                remote_preference="Any",
                is_active=True
            )
            db.add(job_filter)
        else:
            job_filter.role_keywords = role_kws
            job_filter.is_active = True

    await db.flush()
    return {
        "message": "Resume path set & parsed successfully! Profile & job filters updated.",
        "path": path,
        "extracted_skills": extracted_skills
    }


# ── Experience CRUD ──────────────────────────────────────────────────────────

@router.post("/experience", response_model=ExperienceResponse)
async def add_experience(data: ExperienceCreate, db: AsyncSession = Depends(get_db)):
    """Add a work experience entry."""
    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Create a profile first.")
    exp = Experience(profile_id=profile.id, **data.model_dump())
    db.add(exp)
    await db.flush()
    await db.refresh(exp)
    return exp


@router.get("/experience", response_model=list[ExperienceResponse])
async def list_experiences(db: AsyncSession = Depends(get_db)):
    """List all work experiences."""
    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        return []
    result = await db.execute(
        select(Experience).where(Experience.profile_id == profile.id)
    )
    return result.scalars().all()


@router.delete("/experience/{exp_id}")
async def delete_experience(exp_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a work experience entry."""
    result = await db.execute(select(Experience).where(Experience.id == exp_id))
    exp = result.scalar_one_or_none()
    if not exp:
        raise HTTPException(status_code=404, detail="Experience not found.")
    await db.delete(exp)
    return {"message": "Experience deleted"}


# ── Education CRUD ───────────────────────────────────────────────────────────

@router.post("/education", response_model=EducationResponse)
async def add_education(data: EducationCreate, db: AsyncSession = Depends(get_db)):
    """Add an education entry."""
    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Create a profile first.")
    edu = Education(profile_id=profile.id, **data.model_dump())
    db.add(edu)
    await db.flush()
    await db.refresh(edu)
    return edu


@router.get("/education", response_model=list[EducationResponse])
async def list_education(db: AsyncSession = Depends(get_db)):
    """List all education entries."""
    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        return []
    result = await db.execute(
        select(Education).where(Education.profile_id == profile.id)
    )
    return result.scalars().all()


@router.delete("/education/{edu_id}")
async def delete_education(edu_id: int, db: AsyncSession = Depends(get_db)):
    """Delete an education entry."""
    result = await db.execute(select(Education).where(Education.id == edu_id))
    edu = result.scalar_one_or_none()
    if not edu:
        raise HTTPException(status_code=404, detail="Education not found.")
    await db.delete(edu)
    return {"message": "Education deleted"}


# ── Skills CRUD ──────────────────────────────────────────────────────────────

@router.post("/skill", response_model=SkillResponse)
async def add_skill(data: SkillCreate, db: AsyncSession = Depends(get_db)):
    """Add a skill."""
    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Create a profile first.")
    skill = Skill(profile_id=profile.id, **data.model_dump())
    db.add(skill)
    await db.flush()
    await db.refresh(skill)
    return skill


@router.get("/skills", response_model=list[SkillResponse])
async def list_skills(db: AsyncSession = Depends(get_db)):
    """List all skills."""
    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        return []
    result = await db.execute(
        select(Skill).where(Skill.profile_id == profile.id)
    )
    return result.scalars().all()


@router.delete("/skill/{skill_id}")
async def delete_skill(skill_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a skill."""
    result = await db.execute(select(Skill).where(Skill.id == skill_id))
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found.")
    await db.delete(skill)
    return {"message": "Skill deleted"}
