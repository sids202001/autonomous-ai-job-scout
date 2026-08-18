"""
AutoApply - Applications Router
Application tracking, batch application, application logs, and auto-fill endpoints.
"""
import os
import json
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.models import Application, Job, Profile
from app.schemas import ApplicationCreate, ApplicationUpdate, ApplicationResponse
from app.services.auto_filler import AutoFiller

logger = logging.getLogger(__name__)
router = APIRouter()
auto_filler = AutoFiller()


class BatchApplyRequest(BaseModel):
    job_ids: list[int]
    profile_id: int = 1


@router.post("/", response_model=ApplicationResponse)
async def create_application(data: ApplicationCreate, db: AsyncSession = Depends(get_db)):
    """Save a job to applications (status: saved)."""
    job = (await db.execute(select(Job).where(Job.id == data.job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    existing = (await db.execute(
        select(Application).where(
            Application.job_id == data.job_id,
            Application.profile_id == data.profile_id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Application already exists for this job.")

    app = Application(**data.model_dump())
    db.add(app)
    await db.flush()

    result = await db.execute(
        select(Application).options(selectinload(Application.job)).where(Application.id == app.id)
    )
    return result.scalar_one()


@router.get("/", response_model=list[ApplicationResponse])
async def list_applications(
    status: str = Query(None),
    sort_by: str = Query("created_at"),
    db: AsyncSession = Depends(get_db),
):
    """List all applications with optional status filter."""
    query = select(Application).options(selectinload(Application.job))

    if status:
        query = query.where(Application.status == status)

    if sort_by == "status":
        query = query.order_by(Application.status)
    elif sort_by == "applied_at":
        query = query.order_by(desc(Application.applied_at))
    else:
        query = query.order_by(desc(Application.created_at))

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/log")
async def get_application_log():
    """Return the history log of all application attempts."""
    log_path = "data/application_log.json"
    if not os.path.exists(log_path):
        return []
    logs = []
    try:
        with open(log_path, "r") as f:
            for line in f:
                if line.strip():
                    logs.append(json.loads(line))
    except Exception as e:
        logger.error(f"Error reading application log: {e}")
    return logs


@router.get("/{app_id}", response_model=ApplicationResponse)
async def get_application(app_id: int, db: AsyncSession = Depends(get_db)):
    """Get application details."""
    result = await db.execute(
        select(Application).options(selectinload(Application.job)).where(Application.id == app_id)
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found.")
    return app


@router.put("/{app_id}", response_model=ApplicationResponse)
async def update_application(
    app_id: int, data: ApplicationUpdate, db: AsyncSession = Depends(get_db)
):
    """Update application status, notes, etc."""
    result = await db.execute(select(Application).where(Application.id == app_id))
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found.")

    update_data = data.model_dump(exclude_none=True)
    if "status" in update_data and update_data["status"] == "applied":
        update_data["applied_at"] = datetime.now(timezone.utc)

    for key, value in update_data.items():
        setattr(app, key, value)

    await db.flush()
    await db.refresh(app)
    return app


@router.delete("/{app_id}")
async def delete_application(app_id: int, db: AsyncSession = Depends(get_db)):
    """Delete an application."""
    result = await db.execute(select(Application).where(Application.id == app_id))
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found.")
    await db.delete(app)
    return {"message": "Application deleted"}


@router.post("/{app_id}/autofill")
async def trigger_autofill(
    app_id: int,
    mode: str = Query("review", description="Application mode: 'review', 'auto_submit', or 'batch'"),
    db: AsyncSession = Depends(get_db)
):
    """
    Trigger auto-fill for an application with specified mode.
    Modes:
        - 'review': Fills form, takes screenshot, leaves browser open
        - 'auto_submit': Fills form, screenshots, submits, logs result, closes
        - 'batch': Used for automated queue processing
    """
    result = await db.execute(
        select(Application).options(selectinload(Application.job)).where(Application.id == app_id)
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found.")

    profile = (await db.execute(
        select(Profile).where(Profile.id == app.profile_id)
    )).scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=400, detail="Profile not found.")

    app.status = "applying"
    app.apply_mode = mode
    await db.flush()

    import asyncio
    asyncio.create_task(auto_filler.open_and_prefill(app.job.url, profile, mode=mode))

    return {
        "message": f"🚀 Browser opening in '{mode}' mode! Pre-filling form with your profile data.",
        "status": "applying",
        "mode": mode,
    }


@router.post("/batch")
async def batch_apply(request: BatchApplyRequest, db: AsyncSession = Depends(get_db)):
    """Queue multiple jobs for batch auto-apply in 'batch' mode."""
    profile = (await db.execute(select(Profile).where(Profile.id == request.profile_id))).scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=400, detail="Profile not found.")

    results = []
    for job_id in request.job_ids:
        job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
        if job:
            existing = (await db.execute(
                select(Application).where(
                    Application.job_id == job_id,
                    Application.profile_id == request.profile_id,
                )
            )).scalar_one_or_none()
            if not existing:
                app = Application(job_id=job_id, profile_id=request.profile_id, status="applying", apply_mode="batch")
                db.add(app)
            results.append({"job_id": job_id, "title": job.title, "queued": True})

    await db.flush()

    import asyncio
    async def process_batch():
        for job_id in request.job_ids:
            from app.database import async_session
            async with async_session() as session:
                job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
                if job:
                    await auto_filler.open_and_prefill(job.url, profile, mode="batch")
                    await asyncio.sleep(5)

    asyncio.create_task(process_batch())
    return {"message": f"Batch application started for {len(results)} jobs.", "jobs": results}
