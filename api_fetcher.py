import asyncio
from datetime import datetime, timezone
import httpx
import logging
from typing import List, Dict, Any
from bs4 import BeautifulSoup
from config import REAL_COMPANY_BOARDS

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

_BOARD_API_SEMAPHORE = asyncio.Semaphore(3)

def strip_html(html_text: str) -> str:
    """Utility to clean raw HTML descriptions."""
    if not html_text:
        return ""
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        return soup.get_text(separator="\n").strip()
    except Exception:
        return html_text

async def fetch_greenhouse(client: httpx.AsyncClient, company: str, handle: str) -> List[Dict[str, Any]]:
    """Fetch jobs from Greenhouse API with date extraction (max 3 concurrent, releases response immediately)."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{handle}/jobs?content=true"
    jobs = []
    try:
        async with _BOARD_API_SEMAPHORE:
            r = await client.get(url, timeout=15.0)
            r.raise_for_status()
            data = r.json()
            del r  # Release HTTP response body immediately

        for job in data.get("jobs", []):
            job_id = job.get('id')
            # Direct application form URL (bypasses marketing landing page redirects)
            direct_form_url = f"https://job-boards.greenhouse.io/embed/job_app?for={handle}&token={job_id}"
            updated_at = job.get("updated_at")
            posted_date = updated_at[:10] if updated_at else None
            jobs.append({
                "id": f"gh_{handle}_{job_id}",
                "company": company,
                "title": job.get("title", ""),
                "location": job.get("location", {}).get("name", "Unknown"),
                "url": direct_form_url,
                "description_raw": strip_html(job.get("content", "")),
                "source": "greenhouse",
                "posted_date": posted_date,
                "date_unknown": (posted_date is None),
            })
        del data  # Drop in-memory parsed JSON
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        logger.warning(f"Failed to fetch Greenhouse for {company}: {e}")
    except Exception as e:
        logger.warning(f"Error parsing Greenhouse for {company}: {e}")
    return jobs

async def fetch_lever(client: httpx.AsyncClient, company: str, handle: str) -> List[Dict[str, Any]]:
    """Fetch jobs from Lever API with date extraction (max 3 concurrent, releases response immediately)."""
    url = f"https://api.lever.co/v0/postings/{handle}?mode=json"
    jobs = []
    try:
        async with _BOARD_API_SEMAPHORE:
            r = await client.get(url, timeout=15.0)
            r.raise_for_status()
            data = r.json()
            del r  # Release HTTP response body immediately

        for job in data:
            hosted_url = (job.get("hostedUrl") or "").rstrip('/')
            direct_form_url = f"{hosted_url}/apply" if hosted_url and not hosted_url.endswith("/apply") else hosted_url
            created_at = job.get("createdAt")
            if created_at:
                try:
                    posted_date = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                except Exception:
                    posted_date = None
            else:
                posted_date = None
            jobs.append({
                "id": f"lv_{handle}_{job.get('id')}",
                "company": company,
                "title": job.get("text", ""),
                "location": job.get("categories", {}).get("location", "Unknown"),
                "url": direct_form_url,
                "description_raw": strip_html(job.get("descriptionPlain", "") or job.get("description", "")),
                "source": "lever",
                "posted_date": posted_date,
                "date_unknown": (posted_date is None),
            })
        del data  # Drop in-memory parsed JSON
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        logger.warning(f"Failed to fetch Lever for {company}: {e}")
    except Exception as e:
        logger.warning(f"Error parsing Lever for {company}: {e}")
    return jobs

async def fetch_ashby(client: httpx.AsyncClient, company: str, handle: str) -> List[Dict[str, Any]]:
    """Fetch jobs from Ashby API with date extraction (max 3 concurrent, releases response immediately)."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{handle}"
    jobs = []
    try:
        async with _BOARD_API_SEMAPHORE:
            r = await client.get(url, timeout=15.0)
            r.raise_for_status()
            data = r.json()
            del r  # Release HTTP response body immediately

        for job in data.get("jobs", []):
            raw_url = job.get("applyUrl") or job.get("jobUrl", "")
            direct_form_url = raw_url
            if raw_url and not raw_url.endswith("/application") and "/application" not in raw_url:
                direct_form_url = f"{raw_url.rstrip('/')}/application"
            published_at = job.get("publishedAt")
            posted_date = published_at[:10] if published_at else None
            jobs.append({
                "id": f"ashby_{handle}_{job.get('id')}",
                "company": company,
                "title": job.get("title", ""),
                "location": job.get("location", "Unknown") if isinstance(job.get("location"), str) else (job.get("location") or {}).get("name", "Unknown"),
                "url": direct_form_url,
                "description_raw": strip_html(job.get("descriptionPlain", "") or job.get("descriptionHtml", "")),
                "source": "ashby",
                "posted_date": posted_date,
                "date_unknown": (posted_date is None),
            })
        del data  # Drop in-memory parsed JSON
    except Exception as e:
        logger.warning(f"Failed to fetch Ashby for {company}: {e}")
    return jobs

async def fetch_smartrecruiters(client: httpx.AsyncClient, company: str, handle: str) -> List[Dict[str, Any]]:
    """Fetch jobs from SmartRecruiters API (max 3 concurrent, releases response immediately)."""
    url = f"https://api.smartrecruiters.com/v1/companies/{handle}/postings"
    jobs = []
    try:
        async with _BOARD_API_SEMAPHORE:
            r = await client.get(url, timeout=15.0)
            r.raise_for_status()
            data = r.json()
            del r  # Release HTTP response body immediately

        for job in data.get("content", []):
            job_id = job.get("id")
            direct_form_url = f"https://jobs.smartrecruiters.com/{handle}/{job_id}"
            loc = job.get("location", {})
            loc_str = f"{loc.get('city', '')}, {loc.get('region', '')}, {loc.get('country', '')}".strip(", ")
            jobs.append({
                "id": f"sr_{handle}_{job_id}",
                "company": company,
                "title": job.get("name", ""),
                "location": loc_str or "Unknown",
                "url": direct_form_url,
                "description_raw": "",
                "source": "smartrecruiters"
            })
        del data
    except Exception as e:
        logger.warning(f"Failed to fetch SmartRecruiters for {company}: {e}")
    return jobs

async def fetch_rippling(client: httpx.AsyncClient, company: str, handle: str) -> List[Dict[str, Any]]:
    """Fetch jobs from Rippling ATS API (max 3 concurrent, releases response immediately)."""
    url = f"https://api.rippling.com/platform/api/ats/v1/board/{handle}/jobs"
    jobs = []
    try:
        async with _BOARD_API_SEMAPHORE:
            r = await client.get(url, timeout=15.0)
            r.raise_for_status()
            data = r.json()
            del r  # Release HTTP response body immediately

        for job in data:
            direct_form_url = job.get("url") or f"https://ats.rippling.com/{handle}/jobs/{job.get('uuid')}"
            loc = job.get("workLocation", {})
            loc_str = loc.get("label", "Unknown") if isinstance(loc, dict) else str(loc)
            jobs.append({
                "id": f"rippling_{handle}_{job.get('uuid')}",
                "company": company,
                "title": job.get("name", ""),
                "location": loc_str,
                "url": direct_form_url,
                "description_raw": "",
                "source": "rippling"
            })
        del data
    except Exception as e:
        logger.warning(f"Failed to fetch Rippling for {company}: {e}")
    return jobs

async def fetch_company_jobs_direct(company_query: str) -> List[Dict[str, Any]]:
    """Fetch direct ATS jobs for a given company name if in registry."""
    q = company_query.strip().lower()
    targets = []
    for comp, ats_type, handle in REAL_COMPANY_BOARDS:
        c_low = comp.lower()
        if q == c_low:
            targets.append((comp, ats_type, handle))
        elif len(c_low) >= 4 and c_low in q:
            targets.append((comp, ats_type, handle))
        elif len(q) >= 4 and q in c_low:
            targets.append((comp, ats_type, handle))
            
    if not targets:
        return []
        
    results = []
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True, verify=False) as client:
        tasks = []
        for comp, ats_type, handle in targets:
            if ats_type == "greenhouse":
                tasks.append(fetch_greenhouse(client, comp, handle))
            elif ats_type == "lever":
                tasks.append(fetch_lever(client, comp, handle))
            elif ats_type == "ashby":
                tasks.append(fetch_ashby(client, comp, handle))
                
        out = await asyncio.gather(*tasks, return_exceptions=True)
        for res in out:
            if isinstance(res, list):
                results.extend(res)
    return results

async def fetch_all_jobs(max_total_jobs: int = 500) -> List[Dict[str, Any]]:
    """Concurrently fetch all jobs from all configured ATS boards with max 3 concurrency, capping in-memory queue."""
    all_jobs = []
    
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True, verify=False) as client:
        tasks = []
        for company, ats_type, handle in REAL_COMPANY_BOARDS:
            if ats_type == "greenhouse":
                tasks.append(fetch_greenhouse(client, company, handle))
            elif ats_type == "lever":
                tasks.append(fetch_lever(client, company, handle))
            elif ats_type == "ashby":
                tasks.append(fetch_ashby(client, company, handle))
        
        for fut in asyncio.as_completed(tasks):
            try:
                res = await fut
                if isinstance(res, list):
                    all_jobs.extend(res)
                    if len(all_jobs) >= max_total_jobs:
                        break
            except Exception as e:
                logger.error(f"Task failed with exception: {e}")
                
    return all_jobs[:max_total_jobs]
