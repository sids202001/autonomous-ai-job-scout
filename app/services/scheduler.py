"""
AutoApply - Background Job Scheduler
Runs periodic job scans using APScheduler.
"""
import logging
import asyncio
from datetime import datetime, timezone
import httpx
from bs4 import BeautifulSoup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
_scan_job_id = "auto_scan_jobs"
_discovery_job_id = "auto_discovery_jobs"
_hourly_apply_job_id = "hourly_apply_cycle"


async def run_scheduled_discovery():
    """Background task: discover new companies from official showcase and directories, probe reachability."""
    from app.services.discovery_engine import discovery_engine
    logger.info("=== Scheduled company discovery starting ===")
    try:
        res = await discovery_engine.run_discovery_cycle(probe_batch_size=15)
        logger.info(f"=== Scheduled discovery complete: {res} ===")
    except Exception as e:
        logger.error(f"Scheduled discovery failed: {e}")


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
            new_count = await scraper.scrape_and_save(db, filters)


            matcher = JobMatcher()
            scored = await matcher.score_all_jobs(db, profile.id)

            logger.info(f"=== Scheduled scan complete: {new_count} new jobs, {scored} scored ===")
    except Exception as e:
        logger.error(f"Scheduled scan failed: {e}")


async def run_hourly_application_cycle() -> dict:
    """
    Part 2: Hourly scan cycle across ALL reachable companies in the registry.

    Each hour:
    1. Check global 50/day cap (politeness.can_apply_today())
    2. Fetch fresh postings across all reachable companies (Greenhouse, Lever, Ashby)
    3. Run every existing gate exactly as-is:
       - Dedup (never re-apply the same role)
       - Location (US-only / US remote)
       - Experience 0-3 or '3+', no senior/staff/principal
       - Skill overlap (>=2 skill overlap) & Relevance (no forbidden domains)
       - Per-company 2-per-180-days cap & employer lockouts
    4. Rank survivors with existing yield/fit ranking (fit_ranker)
    5. Submit the single best match (quota never overrides a gate)
    6. If nothing was submitted that hour, send one Telegram update:
       a single short line stating no application was found to submit this hour.
    """
    from app.services.politeness import politeness
    from app.services.telegram_notifier import send_hourly_no_application_telegram
    from config import settings

    logger.info("=== Hourly scan cycle starting across ALL reachable companies ===")

    stats = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_boards": 0,
        "boards_scanned": 0,
        "per_board_postings": {},
        "postings_found": 0,
        "duplicate_in_scan_blocked": 0,
        "dedup_blocked": 0,
        "location_blocked": 0,
        "experience_blocked": 0,
        "relevance_blocked": 0,
        "skill_overlap_blocked": 0,
        "company_cap_blocked": 0,
        "survivors_count": 0,
        "submitted": False,
        "selected_job": None,
        "terminal_outcomes": [],
    }

    # 1. Global 50/day application cap check
    if not politeness.can_apply_today():
        logger.warning(f"Hourly cycle skipped: Global daily application cap reached ({settings.MAX_APPLICATIONS_PER_DAY}/day).")
        await send_hourly_no_application_telegram()
        return stats

    # 2. Fetch candidate profile
    from app.database import async_session
    from app.models import Profile
    from sqlalchemy import select

    async with async_session() as db:
        profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()

    if not profile:
        logger.warning("Hourly cycle: No profile found in database. Skipping submission.")
        await send_hourly_no_application_telegram()
        return stats

    # 3. Retrieve all reachable companies from registry
    from app.services.company_registry import company_registry
    try:
        with company_registry._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT name, ats_type, canonical_domain FROM company_registry "
                "WHERE reachability = 'reachable' AND ats_type IN ('greenhouse', 'lever', 'ashby')"
            )
            boards = [(row[0], row[1], row[2]) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"Hourly cycle failed querying company_registry: {e}")
        await send_hourly_no_application_telegram()
        return stats

    stats["total_boards"] = len(boards)
    logger.info(f"Hourly cycle: Found {len(boards)} reachable boards in registry.")

    # 4. Concurrently fetch postings
    raw_jobs = []
    sem = asyncio.Semaphore(15)

    def _strip_html(text: str) -> str:
        if not text:
            return ""
        try:
            soup = BeautifulSoup(text, "html.parser")
            return soup.get_text(separator="\n").strip()
        except Exception:
            return text

    async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
        async def _fetch_board(company_name: str, ats_type: str, handle: str):
            board_key = f"{company_name} ({ats_type})"
            board_found = 0
            async with sem:
                try:
                    if ats_type == "greenhouse":
                        url = f"https://boards-api.greenhouse.io/v1/boards/{handle}/jobs?content=true"
                        r = await client.get(url)
                        if r.status_code == 200:
                            data = r.json()
                            for item in data.get("jobs", []):
                                title = item.get("title", "").strip()
                                job_url = item.get("absolute_url", "").strip()
                                if not title or not job_url:
                                    continue
                                location = item.get("location", {}).get("name", "Remote / Various")
                                content = _strip_html(item.get("content", ""))
                                raw_jobs.append({
                                    "id": str(item.get("id", "")),
                                    "title": title,
                                    "company": company_name,
                                    "location": location,
                                    "url": job_url,
                                    "description": content[:3000] if content else f"Role at {company_name}.",
                                    "posted_date": item.get("updated_at", "")[:10] if item.get("updated_at") else None,
                                    "source": f"{company_name} (Greenhouse)",
                                })
                                board_found += 1
                            stats["boards_scanned"] += 1
                    elif ats_type == "lever":
                        url = f"https://api.lever.co/v0/postings/{handle}?mode=json"
                        r = await client.get(url)
                        if r.status_code == 200:
                            data = r.json()
                            for item in data:
                                title = item.get("text", "").strip()
                                job_url = item.get("hostedUrl", "").strip() or item.get("applyUrl", "").strip()
                                if not title or not job_url:
                                    continue
                                cats = item.get("categories", {})
                                location = cats.get("location") or "Remote / Various"
                                content = item.get("descriptionPlain") or ""
                                raw_jobs.append({
                                    "id": str(item.get("id", "")),
                                    "title": title,
                                    "company": company_name,
                                    "location": location,
                                    "url": job_url,
                                    "description": content[:3000] if content else f"Role at {company_name}.",
                                    "posted_date": None,
                                    "source": f"{company_name} (Lever)",
                                })
                                board_found += 1
                            stats["boards_scanned"] += 1
                    elif ats_type == "ashby":
                        url = f"https://api.ashbyhq.com/posting-api/job-board/{handle}"
                        r = await client.get(url)
                        if r.status_code == 200:
                            data = r.json()
                            for item in data.get("jobs", []):
                                title = item.get("title", "").strip()
                                job_url = item.get("jobUrl", "").strip() or item.get("applyUrl", "").strip()
                                if not title or not job_url:
                                    continue
                                location = item.get("location") or "Remote / Various"
                                content = item.get("descriptionPlain") or ""
                                raw_jobs.append({
                                    "id": str(item.get("id", "")),
                                    "title": title,
                                    "company": company_name,
                                    "location": location,
                                    "url": job_url,
                                    "description": content[:3000] if content else f"Role at {company_name}.",
                                    "posted_date": item.get("publishedAt", "")[:10] if item.get("publishedAt") else None,
                                    "source": f"{company_name} (Ashby)",
                                })
                                board_found += 1
                            stats["boards_scanned"] += 1
                    logger.info(f"Board scan [{ats_type}] {company_name}: {board_found} postings found.")
                    stats["per_board_postings"][board_key] = board_found
                except Exception as fe:
                    logger.debug(f"Hourly fetch notice for {company_name} ({ats_type}): {fe}")
                    stats["per_board_postings"][board_key] = 0

        await asyncio.gather(*(_fetch_board(n, a, s) for n, a, s in boards))

    stats["postings_found"] = len(raw_jobs)
    logger.info(f"Hourly cycle: Discovered {len(raw_jobs)} raw postings across {stats['boards_scanned']} reachable boards.")

    # 5. Evaluate all postings through every gate with exact accounting
    from app.services.dedup_gate import DedupGate
    from app.services.location_gate import is_us_location
    from app.services.experience_gate import ExperienceGate
    from app.services.relevance_gate import RelevanceGate
    from app.services.fit_ranker import fit_ranker
    from app.services.company_cap import company_cap_tracker

    survivors = []
    seen_urls = set()

    for job in raw_jobs:
        url = job["url"]
        company = job["company"]
        title = job["title"]
        desc = job["description"]
        location = job["location"]

        if url in seen_urls:
            stats["duplicate_in_scan_blocked"] += 1
            stats["terminal_outcomes"].append({
                "company": company, "title": title, "url": url,
                "outcome": "SKIPPED", "gate": "duplicate_in_scan", "reason": "Duplicate URL in same hourly scan"
            })
            continue
        seen_urls.add(url)

        # Gate A: Dedup (never re-apply the same role)
        dedup_status, dedup_reason = DedupGate.evaluate_job(company, url)
        if dedup_status in ("actioned", "stale_rejected"):
            stats["dedup_blocked"] += 1
            stats["terminal_outcomes"].append({
                "company": company, "title": title, "url": url,
                "outcome": "SKIPPED", "gate": "dedup_gate", "reason": f"{dedup_status}: {dedup_reason}"
            })
            continue

        # Gate B: US-only Location Gate
        if not is_us_location(location, title, desc):
            stats["location_blocked"] += 1
            DedupGate.mark_evaluated(company, url, title, outcome="rejected", reason="location_non_us")
            stats["terminal_outcomes"].append({
                "company": company, "title": title, "url": url,
                "outcome": "SKIPPED", "gate": "location_gate", "reason": f"Non-US location: '{location}'"
            })
            continue

        # Gate C: Experience Gate (0-3 or '3+', no senior/staff/principal)
        exp_ok, exp_reason, _ = ExperienceGate.evaluate(title, desc)
        if not exp_ok:
            stats["experience_blocked"] += 1
            DedupGate.mark_evaluated(company, url, title, outcome="rejected", reason=f"experience_{exp_reason}")
            stats["terminal_outcomes"].append({
                "company": company, "title": title, "url": url,
                "outcome": "SKIPPED", "gate": "experience_gate", "reason": exp_reason
            })
            continue

        # Gate D: Relevance Gate (forbidden domains & role family)
        rel_ok, rel_reason, _ = RelevanceGate.evaluate(title, desc)
        if not rel_ok:
            stats["relevance_blocked"] += 1
            DedupGate.mark_evaluated(company, url, title, outcome="rejected", reason=f"relevance_{rel_reason}")
            stats["terminal_outcomes"].append({
                "company": company, "title": title, "url": url,
                "outcome": "SKIPPED", "gate": "relevance_gate", "reason": rel_reason
            })
            continue

        # Gate E: Skill Overlap (>=2 skill overlap)
        skill_fit_pct, matched_skills = fit_ranker.calculate_skill_fit(title, desc)
        if len(matched_skills) < 2:
            stats["skill_overlap_blocked"] += 1
            DedupGate.mark_evaluated(company, url, title, outcome="rejected", reason="insufficient_skill_overlap")
            stats["terminal_outcomes"].append({
                "company": company, "title": title, "url": url,
                "outcome": "SKIPPED", "gate": "skill_overlap_gate", "reason": f"Matched skills ({len(matched_skills)}) < 2"
            })
            continue

        # Gate F: Per-Company 2-per-180-days Cap & Employer Lockouts
        is_capped, cap_reason, _ = company_cap_tracker.is_company_capped(company, url)
        if is_capped:
            stats["company_cap_blocked"] += 1
            DedupGate.mark_evaluated(company, url, title, outcome="skipped", reason="company_cap_reached")
            stats["terminal_outcomes"].append({
                "company": company, "title": title, "url": url,
                "outcome": "SKIPPED", "gate": "company_cap_gate", "reason": cap_reason
            })
            continue

        job["fit_score"] = round(skill_fit_pct * fit_ranker.calculate_recency(job.get("posted_date")) * fit_ranker.calculate_h1b_signal(desc), 4)
        job["matched_skills"] = matched_skills
        survivors.append(job)

    stats["survivors_count"] = len(survivors)
    logger.info(
        f"Hourly cycle gate results: Postings={stats['postings_found']}, "
        f"DuplicatesInScan={stats['duplicate_in_scan_blocked']}, "
        f"DedupBlocked={stats['dedup_blocked']}, LocationBlocked={stats['location_blocked']}, "
        f"ExpBlocked={stats['experience_blocked']}, RelBlocked={stats['relevance_blocked']}, "
        f"SkillOverlapBlocked={stats['skill_overlap_blocked']}, CapBlocked={stats['company_cap_blocked']}, "
        f"Survivors={stats['survivors_count']}"
    )

    # 6. Rank survivors & submit best match
    if not survivors:
        logger.info("Hourly cycle: 0 postings passed all gates. Submitting nothing.")
        await send_hourly_no_application_telegram()
        return stats

    ranked = fit_ranker.rank_and_order_queue(survivors)
    best_job = ranked[0]
    for other_job in ranked[1:]:
        stats["terminal_outcomes"].append({
            "company": other_job["company"], "title": other_job["title"], "url": other_job["url"],
            "outcome": "SKIPPED", "gate": "rank_survivor_queue",
            "reason": f"Ranked behind top pick ({best_job['company']} - {best_job['title']})"
        })

    stats["selected_job"] = {
        "company": best_job["company"],
        "title": best_job["title"],
        "url": best_job["url"],
        "fit_score": best_job.get("fit_score", 0.0),
        "matched_skills": best_job.get("matched_skills", []),
    }
    logger.info(
        f"Hourly cycle best match picked: {best_job['company']} - {best_job['title']} "
        f"({best_job['url']}) with fit score {best_job.get('fit_score', 0.0)}"
    )

    from app.services.auto_filler import auto_filler
    mode = "auto_submit" if not getattr(settings, "DRY_RUN", False) else "review"

    try:
        result = await auto_filler.open_and_prefill(
            job_url=best_job["url"],
            profile=profile,
            mode=mode,
            job_title=best_job["title"],
            job_description=best_job.get("description", ""),
            company=best_job["company"],
            job_id=best_job.get("id"),
            hitl_enabled=False,
        )
        if result.get("status") == "submitted" or result.get("success"):
            stats["submitted"] = True
            politeness.record_application()
            DedupGate.mark_actioned(best_job["company"], best_job["url"], best_job["title"], action="applied")
            stats["terminal_outcomes"].append({
                "company": best_job["company"], "title": best_job["title"], "url": best_job["url"],
                "outcome": "SUBMITTED", "gate": "none", "reason": result.get("message", "4-evidence verified submission")
            })
            logger.info(f"Hourly cycle verified submission completed: {best_job['company']} - {best_job['title']}")
        else:
            term_status = "BLOCKED" if "challenge" in result.get("status", "").lower() else "FAILED"
            stats["terminal_outcomes"].append({
                "company": best_job["company"], "title": best_job["title"], "url": best_job["url"],
                "outcome": term_status, "gate": "form_submission", "reason": result.get("message", "Submission verification failed")
            })
            logger.info(f"Hourly cycle application not submitted ({result.get('status')}: {result.get('message')}).")
            await send_hourly_no_application_telegram()
    except Exception as ae:
        stats["terminal_outcomes"].append({
            "company": best_job["company"], "title": best_job["title"], "url": best_job["url"],
            "outcome": "FAILED", "gate": "exception", "reason": str(ae)
        })
        logger.error(f"Hourly cycle submission error: {ae}")
        await send_hourly_no_application_telegram()

    return stats


def start_scheduler(interval_hours: int = 4, discovery_interval_hours: int = 6, hourly_apply_interval_hours: int = 1):
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
    scheduler.add_job(
        run_scheduled_discovery,
        trigger=IntervalTrigger(hours=discovery_interval_hours),
        id=_discovery_job_id,
        replace_existing=True,
        name=f"Auto-discovery every {discovery_interval_hours} hours",
    )
    scheduler.add_job(
        run_hourly_application_cycle,
        trigger=IntervalTrigger(hours=hourly_apply_interval_hours),
        id=_hourly_apply_job_id,
        replace_existing=True,
        name=f"Hourly application cycle every {hourly_apply_interval_hours} hour(s)",
    )
    scheduler.start()
    logger.info("[DISCOVERY RUN-LOCK] Discovery loop active via scheduler.py APScheduler (guarded by global run-lock)")
    logger.info(f"Scheduler started: scanning every {interval_hours}h, discovery every {discovery_interval_hours}h, hourly apply cycle every {hourly_apply_interval_hours}h")


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
