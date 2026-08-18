"""
AutoApply - Multi-Platform Job Scraper Service
Fetches real, live job postings from:
1. Greenhouse API (50+ tech companies)
2. Lever API (10+ tech companies)
3. jobspy library (LinkedIn, Indeed, Glassdoor, ZipRecruiter)

STRICT EXPERIENCE RULE: Filters out jobs requiring >3 years of experience.
"""
import logging
import re
import asyncio
import hashlib
from datetime import datetime, timezone
import httpx
from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import Job, JobFilter

logger = logging.getLogger(__name__)

# 70+ top H-1B sponsoring tech companies using public Greenhouse / Lever APIs
REAL_COMPANY_BOARDS = [
    # Top Tier Tech & Fintech
    ("Stripe", "greenhouse", "stripe"),
    ("Airbnb", "greenhouse", "airbnb"),
    ("Spotify", "lever", "spotify"),
    ("Coinbase", "greenhouse", "coinbase"),
    ("Figma", "greenhouse", "figma"),
    ("Cloudflare", "greenhouse", "cloudflare"),
    ("DataDog", "greenhouse", "datadog"),
    ("Elastic", "greenhouse", "elastic"),
    ("Palantir", "lever", "palantir"),
    ("Discord", "greenhouse", "discord"),
    ("Reddit", "greenhouse", "reddit"),
    ("Twilio", "greenhouse", "twilio"),
    
    # AI / ML Pioneers (H-1B Sponsors)
    ("OpenAI", "greenhouse", "openai"),
    ("Anthropic", "greenhouse", "anthropic"),
    ("Scale AI", "lever", "scaleai"),
    ("Cohere", "lever", "cohere"),
    ("Hugging Face", "greenhouse", "huggingface"),
    ("Stability AI", "lever", "stabilityai"),
    ("Perplexity AI", "lever", "perplexity"),
    ("Character AI", "greenhouse", "character"),
    ("Databricks", "greenhouse", "databricks"),
    
    # Cloud, Database & Infra
    ("Snowflake", "greenhouse", "snowflakecomputing"),
    ("HashiCorp", "greenhouse", "hashicorp"),
    ("MongoDB", "greenhouse", "mongodb"),
    ("Confluent", "greenhouse", "confluent"),
    ("Vercel", "greenhouse", "vercel"),
    ("Supabase", "greenhouse", "supabase"),
    ("Rubrik", "greenhouse", "rubrik"),
    ("Cohesity", "greenhouse", "cohesity"),
    ("Samsara", "greenhouse", "samsara"),
    ("Okta", "greenhouse", "okta"),
    
    # FinTech & Modern Finance
    ("Plaid", "lever", "plaid"),
    ("Robinhood", "greenhouse", "robinhood"),
    ("Block", "greenhouse", "block"),
    ("Brex", "greenhouse", "brex"),
    ("Ramp", "greenhouse", "ramp"),
    ("Affirm", "greenhouse", "affirm"),
    ("SoFi", "greenhouse", "sofi"),
    ("Bill.com", "greenhouse", "bill"),
    
    # Enterprise & Productivity SaaS
    ("Notion", "greenhouse", "notion"),
    ("Linear", "lever", "linear"),
    ("Retool", "greenhouse", "retool"),
    ("Airtable", "greenhouse", "airtable"),
    ("Rippling", "greenhouse", "rippling"),
    ("Gusto", "greenhouse", "gusto"),
    ("Webflow", "greenhouse", "webflow"),
    ("Zapier", "greenhouse", "zapier"),
    ("Atlassian", "greenhouse", "atlassian"),
    ("Asana", "greenhouse", "asana"),
    ("GitLab", "greenhouse", "gitlab"),
    ("Miro", "greenhouse", "miro"),
    ("Canva", "greenhouse", "canva"),
    ("Braze", "greenhouse", "braze"),
    ("Klaviyo", "greenhouse", "klaviyo"),
    ("PagerDuty", "greenhouse", "pagerduty"),
    
    # Security & DevOps
    ("CrowdStrike", "greenhouse", "crowdstrike"),
    ("Snyk", "greenhouse", "snyk"),
    ("Grafana Labs", "greenhouse", "grafanalabs"),
    
    # Consumer Tech & Marketplaces
    ("DoorDash", "greenhouse", "doordash"),
    ("Instacart", "greenhouse", "instacart"),
    ("Lyft", "greenhouse", "lyft"),
    ("Pinterest", "greenhouse", "pinterest"),
    ("Snap", "greenhouse", "snap"),
    ("Dropbox", "greenhouse", "dropbox"),
    ("Roblox", "greenhouse", "roblox"),
    ("Etsy", "greenhouse", "etsy"),
    ("Wayfair", "greenhouse", "wayfair"),
    ("Zillow", "greenhouse", "zillow"),
    ("Chewy", "greenhouse", "chewy"),
    
    # HealthTech & Life Sciences
    ("Tempus AI", "greenhouse", "tempus"),
    ("Flatiron Health", "greenhouse", "flatironhealth"),
    ("Oscar Health", "greenhouse", "oscar"),
    ("Color Health", "greenhouse", "color"),
    
    # Autonomous Systems
    ("Waymo", "greenhouse", "waymo"),
    ("Cruise", "greenhouse", "cruise"),
    ("Zoox", "greenhouse", "zoox"),
    ("Aurora Innovation", "greenhouse", "aurora")
]

EXCLUDED_KEYWORDS = [
    "senior", "sr.", "sr ", "staff", "principal", "lead", "director", "head of",
    "vp ", "manager", "architect", "5+ years", "6+ years", "7+ years", "8+ years",
    "10+ years", "4+ years", "5-7 years", "6-8 years", "7-10 years"
]


class JobScraper:
    """
    Multi-platform job scraper combining Greenhouse, Lever, and jobspy sources.
    Enforces maximum 3 years experience limit.
    """

    def _strip_html(self, html_text: str) -> str:
        if not html_text:
            return ""
        try:
            soup = BeautifulSoup(html_text, "html.parser")
            return soup.get_text(separator="\n").strip()
        except Exception:
            return html_text

    def _is_suitable_experience(self, title: str, description: str) -> bool:
        text_lower = f"{title} {description}".lower()
        for kw in EXCLUDED_KEYWORDS:
            if kw in text_lower:
                return False
        match = re.search(r'(\d+)\+?\s*years?\s*(?:of)?\s*experience', text_lower)
        if match:
            try:
                if int(match.group(1)) > 3:
                    return False
            except ValueError:
                pass
        return True

    def _url_hash(self, url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()

    async def _fetch_greenhouse_jobs(self, client: httpx.AsyncClient, company_name: str, handle: str) -> list[dict]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{handle}/jobs?content=true"
        jobs = []
        try:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15.0)
            if r.status_code != 200:
                return jobs
            data = r.json()
            for item in data.get("jobs", []):
                title = item.get("title", "").strip()
                job_url = item.get("absolute_url", "").strip()
                if not title or not job_url:
                    continue
                location = item.get("location", {}).get("name", "Remote / Various")
                content = self._strip_html(item.get("content", ""))
                if not self._is_suitable_experience(title, content):
                    continue
                loc_lower = location.lower()
                title_lower = title.lower()
                remote_type = "Remote" if "remote" in loc_lower or "remote" in title_lower else ("Hybrid" if "hybrid" in loc_lower else "Onsite")
                updated_at = item.get("updated_at", "")
                posted_date = updated_at[:10] if updated_at else datetime.now(timezone.utc).strftime("%Y-%m-%d")
                jobs.append({
                    "title": title, "company": company_name, "location": location,
                    "salary_min": None, "salary_max": None, "job_type": "Full-time",
                    "remote_type": remote_type,
                    "description": content[:3000] if content else f"Join {company_name} as a {title}.",
                    "requirements": f"Role at {company_name}. Location: {location}.",
                    "url": job_url, "source": f"{company_name} (Greenhouse)",
                    "posted_date": posted_date,
                })
        except Exception as e:
            logger.error(f"Greenhouse error for {company_name}: {e}")
        return jobs

    async def _fetch_lever_jobs(self, client: httpx.AsyncClient, company_name: str, handle: str) -> list[dict]:
        url = f"https://api.lever.co/v0/postings/{handle}?mode=json"
        jobs = []
        try:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15.0)
            if r.status_code != 200:
                return jobs
            for item in r.json():
                title = item.get("text", "").strip()
                job_url = item.get("hostedUrl", "").strip()
                if not title or not job_url:
                    continue
                categories = item.get("categories", {})
                location = categories.get("location", "Remote / Various")
                commitment = categories.get("commitment", "Full-time")
                desc = item.get("descriptionPlain", "")
                if not self._is_suitable_experience(title, desc):
                    continue
                loc_lower = location.lower()
                remote_type = "Remote" if "remote" in loc_lower else ("Hybrid" if "hybrid" in loc_lower else "Onsite")
                created_at = item.get("createdAt")
                posted_date = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if created_at else datetime.now(timezone.utc).strftime("%Y-%m-%d")
                jobs.append({
                    "title": title, "company": company_name, "location": location,
                    "salary_min": None, "salary_max": None, "job_type": commitment,
                    "remote_type": remote_type,
                    "description": desc[:3000] if desc else f"Join {company_name} as a {title}.",
                    "requirements": f"Role at {company_name}. Location: {location}.",
                    "url": job_url, "source": f"{company_name} (Lever)",
                    "posted_date": posted_date,
                })
        except Exception as e:
            logger.error(f"Lever error for {company_name}: {e}")
        return jobs

    async def _fetch_jobspy_jobs(self, search_term: str, location: str = "USA", results_wanted: int = 50) -> list[dict]:
        """Fetch jobs from LinkedIn/Indeed/Glassdoor/ZipRecruiter via jobspy."""
        jobs = []
        try:
            from jobspy import scrape_jobs
            loop = asyncio.get_event_loop()
            df = await loop.run_in_executor(
                None,
                lambda: scrape_jobs(
                    site_name=["linkedin", "indeed", "glassdoor", "zip_recruiter"],
                    search_term=search_term,
                    location=location,
                    results_wanted=results_wanted,
                    hours_old=48,
                    country_code="usa",
                )
            )
            if df is None or df.empty:
                return jobs

            for _, row in df.iterrows():
                title = str(row.get("title", "")).strip()
                url = str(row.get("job_url", "")).strip()
                company = str(row.get("company", "Unknown")).strip()
                if not title or not url or url == "nan":
                    continue
                desc = str(row.get("description", "")) if row.get("description") else ""
                if not self._is_suitable_experience(title, desc):
                    continue
                location_val = str(row.get("location", "USA"))
                site = str(row.get("site", "jobspy"))
                jobs.append({
                    "title": title, "company": company, "location": location_val,
                    "salary_min": int(row["min_amount"]) if row.get("min_amount") and str(row["min_amount"]) != "nan" else None,
                    "salary_max": int(row["max_amount"]) if row.get("max_amount") and str(row["max_amount"]) != "nan" else None,
                    "job_type": str(row.get("job_type", "Full-time")),
                    "remote_type": "Remote" if "remote" in location_val.lower() else "Onsite",
                    "description": desc[:3000],
                    "requirements": "",
                    "url": url, "source": f"jobspy ({site})",
                    "posted_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                })
            logger.info(f"jobspy returned {len(jobs)} jobs for '{search_term}'")
        except ImportError:
            logger.warning("jobspy not installed. Run: pip install jobspy")
        except Exception as e:
            logger.error(f"jobspy error for '{search_term}': {e}")
        return jobs

    async def search_jobs(self, filters: list[JobFilter], use_jobspy: bool = False) -> list[dict]:
        """Fetch from company career boards (Greenhouse/Lever). Set use_jobspy=True only if external boards needed."""
        all_jobs = []
        seen_urls = set()

        search_keywords = []
        for f in filters:
            if f.role_keywords:
                search_keywords.extend([kw.strip().lower() for kw in f.role_keywords.split(",") if kw.strip()])

        # 1. Direct Company Career Boards (Greenhouse + Lever)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            for company_name, board_type, handle in REAL_COMPANY_BOARDS:
                logger.info(f"Fetching: {company_name} ({board_type})")
                if board_type == "greenhouse":
                    company_jobs = await self._fetch_greenhouse_jobs(client, company_name, handle)
                else:
                    company_jobs = await self._fetch_lever_jobs(client, company_name, handle)

                if search_keywords:
                    for j in company_jobs:
                        text = f"{j['title']} {j['description']}".lower()
                        if any(kw in text for kw in search_keywords):
                            if j['url'] not in seen_urls:
                                seen_urls.add(j['url'])
                                all_jobs.append(j)
                else:
                    for j in company_jobs:
                        if j['url'] not in seen_urls:
                            seen_urls.add(j['url'])
                            all_jobs.append(j)

        # 2. jobspy (LinkedIn, Indeed, Glassdoor, ZipRecruiter) - Disabled by default
        if use_jobspy and search_keywords:
            for kw in search_keywords[:3]:  # Limit to first 3 keywords to avoid rate limits
                jobspy_results = await self._fetch_jobspy_jobs(kw)
                for j in jobspy_results:
                    if j['url'] not in seen_urls:
                        seen_urls.add(j['url'])
                        all_jobs.append(j)

        logger.info(f"Total scraped: {len(all_jobs)} jobs from direct company career sites")
        return all_jobs

    async def scrape_and_save(self, db: AsyncSession, filters: list[JobFilter], use_jobspy: bool = False) -> int:
        jobs_data = await self.search_jobs(filters, use_jobspy=use_jobspy)
        new_count = 0
        for job_data in jobs_data:
            existing = (await db.execute(
                select(Job).where(Job.url == job_data["url"])
            )).scalar_one_or_none()
            if not existing:
                job = Job(**job_data)
                db.add(job)
                new_count += 1
        await db.commit()
        logger.info(f"Saved {new_count} new direct company jobs to database.")
        return new_count

