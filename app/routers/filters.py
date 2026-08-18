"""
AutoApply - Filters Router
Job filter CRUD and activation endpoints.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import JobFilter, Profile
from app.schemas import JobFilterCreate, JobFilterUpdate, JobFilterResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/", response_model=JobFilterResponse)
async def create_filter(data: JobFilterCreate, db: AsyncSession = Depends(get_db)):
    """Create a new job search filter."""
    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=400, detail="Create a profile first.")

    job_filter = JobFilter(profile_id=profile.id, **data.model_dump())
    db.add(job_filter)
    await db.flush()
    await db.refresh(job_filter)
    logger.info(f"Created filter: {job_filter.name}")
    return job_filter


@router.get("/", response_model=list[JobFilterResponse])
async def list_filters(db: AsyncSession = Depends(get_db)):
    """List all job filters."""
    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        return []
    result = await db.execute(
        select(JobFilter).where(JobFilter.profile_id == profile.id)
    )
    return result.scalars().all()


@router.get("/{filter_id}", response_model=JobFilterResponse)
async def get_filter(filter_id: int, db: AsyncSession = Depends(get_db)):
    """Get filter details."""
    result = await db.execute(select(JobFilter).where(JobFilter.id == filter_id))
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="Filter not found.")
    return f


@router.put("/{filter_id}", response_model=JobFilterResponse)
async def update_filter(
    filter_id: int, data: JobFilterUpdate, db: AsyncSession = Depends(get_db)
):
    """Update a filter."""
    result = await db.execute(select(JobFilter).where(JobFilter.id == filter_id))
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="Filter not found.")
    for key, value in data.model_dump(exclude_none=True).items():
        setattr(f, key, value)
    await db.flush()
    await db.refresh(f)
    return f


@router.delete("/{filter_id}")
async def delete_filter(filter_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a filter."""
    result = await db.execute(select(JobFilter).where(JobFilter.id == filter_id))
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="Filter not found.")
    await db.delete(f)
    return {"message": "Filter deleted"}


@router.put("/{filter_id}/activate")
async def activate_filter(filter_id: int, db: AsyncSession = Depends(get_db)):
    """Activate a filter."""
    result = await db.execute(select(JobFilter).where(JobFilter.id == filter_id))
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="Filter not found.")
    f.is_active = True
    await db.flush()
    return {"message": "Filter activated"}


@router.put("/{filter_id}/deactivate")
async def deactivate_filter(filter_id: int, db: AsyncSession = Depends(get_db)):
    """Deactivate a filter."""
    result = await db.execute(select(JobFilter).where(JobFilter.id == filter_id))
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="Filter not found.")
    f.is_active = False
    await db.flush()
    return {"message": "Filter deactivated"}
