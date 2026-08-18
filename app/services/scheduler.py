"""
AutoApply - Background Job Scheduler
Runs periodic job scans using APScheduler.
"""
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
_scan_job_id = "auto_scan_jobs"


async def run_scheduled_scan():
    """Background task: scan for new jobs and score them."""
    from app.database import async_session
    from app.models import Profile, JobFilter
    from app.services.job_scraper import JobScraper
    from app.services.job_matcher import JobMatcher
    from sqlalchemy import select

    logger.info("=== Scheduled job scan starting ===")
    try:
        async with async_session() as db:
            profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
            if not profile:
                logger.warning("No profile found. Skipping scheduled scan.")
                return

            filters = (await db.execute(
                select(JobFilter).where(
                    JobFilter.profile_id == profile.id,
                    JobFilter.is_active == True,
                )
            )).scalars().all()

            if not filters:
                logger.warning("No active filters. Skipping scheduled scan.")
                return

            scraper = JobScraper()
            new_count = await scraper.scrape_and_save(db, filters, use_jobspy=False)


            matcher = JobMatcher()
            scored = await matcher.score_all_jobs(db, profile.id)

            logger.info(f"=== Scheduled scan complete: {new_count} new jobs, {scored} scored ===")
    except Exception as e:
        logger.error(f"Scheduled scan failed: {e}")


def start_scheduler(interval_hours: int = 4):
    """Start the background scheduler."""
    if scheduler.running:
        logger.info("Scheduler already running.")
        return

    scheduler.add_job(
        run_scheduled_scan,
        trigger=IntervalTrigger(hours=interval_hours),
        id=_scan_job_id,
        replace_existing=True,
        name=f"Auto-scan every {interval_hours} hours",
    )
    scheduler.start()
    logger.info(f"Scheduler started: scanning every {interval_hours} hours")


def stop_scheduler():
    """Stop the background scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")


def get_scheduler_status() -> dict:
    """Get current scheduler status."""
    is_running = scheduler.running
    jobs = []
    if is_running:
        for job in scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
            })
    return {"running": is_running, "jobs": jobs}
