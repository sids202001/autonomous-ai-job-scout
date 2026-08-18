"""
AutoApply - Jobs Router
Job listing, searching, and management endpoints.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, asc
from app.database import get_db
from app.models import Job, JobFilter, Application, Profile
from app.schemas import JobResponse, JobListResponse, DashboardStats
from app.services.job_scraper import JobScraper
from app.services.job_matcher import JobMatcher

logger = logging.getLogger(__name__)
router = APIRouter()
scraper = JobScraper()
matcher = JobMatcher()


@router.get("/", response_model=JobListResponse)
async def list_jobs(
    search: str = Query(None),
    source: str = Query(None),
    min_score: float = Query(None),
    sort_by: str = Query("scraped_at"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """List all jobs with optional filters and pagination."""
    query = select(Job).where(Job.is_hidden == False)

    if search:
        query = query.where(
            Job.title.ilike(f"%{search}%") | Job.company.ilike(f"%{search}%")
        )
    if source:
        query = query.where(Job.source == source)
    if min_score is not None:
        query = query.where(Job.match_score >= min_score)

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Sort
    if sort_by == "match_score":
        query = query.order_by(desc(Job.match_score))
    elif sort_by == "posted_date":
        query = query.order_by(desc(Job.posted_date))
    elif sort_by == "salary":
        query = query.order_by(desc(Job.salary_max))
    else:
        query = query.order_by(desc(Job.scraped_at))

    # Paginate
    query = query.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(query)
    jobs = result.scalars().all()

    return JobListResponse(jobs=jobs, total=total, page=page, per_page=per_page)


@router.get("/stats", response_model=DashboardStats)
async def get_stats(db: AsyncSession = Depends(get_db)):
    """Get dashboard statistics."""
    total_jobs = (await db.execute(
        select(func.count()).select_from(Job).where(Job.is_hidden == False)
    )).scalar() or 0

    total_applied = (await db.execute(
        select(func.count()).select_from(Application).where(Application.status == "applied")
    )).scalar() or 0

    total_interviews = (await db.execute(
        select(func.count()).select_from(Application).where(Application.status == "interview")
    )).scalar() or 0

    total_saved = (await db.execute(
        select(func.count()).select_from(Application).where(Application.status == "saved")
    )).scalar() or 0

    total_offers = (await db.execute(
        select(func.count()).select_from(Application).where(Application.status == "offer")
    )).scalar() or 0

    recent_result = await db.execute(
        select(Job).where(Job.is_hidden == False).order_by(desc(Job.scraped_at)).limit(10)
    )
    recent_jobs = recent_result.scalars().all()

    return DashboardStats(
        total_jobs=total_jobs,
        total_applied=total_applied,
        total_interviews=total_interviews,
        total_saved=total_saved,
        total_offers=total_offers,
        recent_jobs=recent_jobs,
    )


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """Get job details by ID."""
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@router.post("/search")
async def trigger_search(db: AsyncSession = Depends(get_db)):
    """Trigger a new job search using active filters."""
    # Get profile
    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=400, detail="Create a profile first.")

    # Get active filters
    result = await db.execute(
        select(JobFilter).where(
            JobFilter.profile_id == profile.id,
            JobFilter.is_active == True,
        )
    )
    filters = result.scalars().all()
    if not filters:
        raise HTTPException(status_code=400, detail="No active filters. Create one first.")

    # Run scraper
    new_count = await scraper.scrape_and_save(db, filters)

    # Run matcher
    scored = await matcher.score_all_jobs(db, profile.id)

    return {
        "message": f"Search complete. Found {new_count} new jobs, scored {scored} jobs.",
        "new_jobs": new_count,
        "scored_jobs": scored,
    }


@router.put("/{job_id}/hide")
async def hide_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """Hide a job from the list."""
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    job.is_hidden = True
    await db.flush()
    return {"message": "Job hidden"}


@router.put("/{job_id}/unhide")
async def unhide_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """Unhide a job."""
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    job.is_hidden = False
    await db.flush()
    return {"message": "Job unhidden"}
