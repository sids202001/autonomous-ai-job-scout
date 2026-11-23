"""
AutoApply - Multi-Platform Job Scraper Service
Fetches real, live job postings from:
1. Greenhouse API (90+ tech companies)
2. Lever API (10+ tech companies)
3. Ashby API (30+ tech companies)

STRICT EXPERIENCE RULE: Filters out jobs requiring >3 years of experience.
"""
import logging
import re
import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
import httpx
from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import Job, JobFilter

logger = logging.getLogger(__name__)


# ── Sponsorship rejection patterns (shared with job_matcher) ─────────
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

CONFIRMED_SPONSORSHIP_PATTERNS = [
    r'(?:visa|h-?1b)\s+sponsorship\s+(?:is\s+)?available',
    r'we\s+(?:do\s+)?sponsor\s+(?:h-?1b\s+)?visas?',
    r'willing\s+to\s+sponsor',
    r'open\s+to\s+sponsoring',
    r'sponsorship\s+(?:is\s+)?provided',
]


@dataclass
class FunnelCounters:
    """Per-run funnel stage counters for instrumentation (FIX 4)."""
    coverage_searchable: int = 0
    coverage_unsupported: int = 0
    coverage_total: int = 442

    companies_in_list: int = 0
    companies_scanned: int = 0
    companies_reachable: int = 0
    companies_unreachable: int = 0
    unreachable_names: List[str] = field(default_factory=list)

    raw_postings: int = 0
    
    # Date window & Location pre-filters (FIX 3 & FIX 4)
    stale_date_dropped: int = 0
    date_unknown_processed: int = 0
    location_non_us_dropped: int = 0

    # Dedup gate (FIX 1: evaluated vs actioned)
    dedup_actioned_blocked: int = 0
    dedup_stale_rejected_blocked: int = 0
    re_admitted_count: int = 0
    dedupe_removed: int = 0
    after_dedupe: int = 0

    # Sponsorship gate (Stage 2)
    sponsor_confirmed: int = 0
    sponsor_unknown: int = 0
    sponsor_explicit_no: int = 0
    after_sponsorship: int = 0

    # Experience gate (Stage 3)
    exp_pass: int = 0
    exp_unstated_pass: int = 0
    exp_seniority_title: int = 0
    exp_exceeded: int = 0
    exp_unparseable: int = 0
    after_experience: int = 0

    # Relevance gate (Stage 4)
    rel_pass: int = 0
    rel_title_guarantee: int = 0
    rel_forbidden: int = 0
    rel_non_target: int = 0
    rel_low_relevance: int = 0
    after_relevance: int = 0

    # Relevance score distribution
    rel_dist_00_19: int = 0
    rel_dist_20_39: int = 0
    rel_dist_40_59: int = 0
    rel_dist_60_79: int = 0
    rel_dist_80_100: int = 0

    # Company-cap check (Stage 5)
    cap_blocked: int = 0
    cap_passed: int = 0

    # Final Eligible
    eligible: int = 0

    def record_relevance_score(self, score: float):
        if score >= 0.80:
            self.rel_dist_80_100 += 1
        elif score >= 0.60:
            self.rel_dist_60_79 += 1
        elif score >= 0.40:
            self.rel_dist_40_59 += 1
        elif score >= 0.20:
            self.rel_dist_20_39 += 1
        else:
            self.rel_dist_00_19 += 1

    def to_table(self) -> str:
        """Render the complete verbatim funnel table."""
        r = self.raw_postings or 1
        pct = lambda n: f"{n/r*100:5.1f}%" if self.raw_postings > 0 else "  N/A"
        cov_s = self.coverage_searchable or self.companies_reachable
        cov_u = self.coverage_unsupported or self.companies_unreachable
        cov_tot = self.coverage_total or (cov_s + cov_u)
        cov_line = f"{cov_s} of {cov_tot} searchable ({cov_u} unsupported)"
        lines = [
            "┌─────────────────────────────────────────────────────┬──────────┬──────────┐",
            "│ Stage                                               │    Count │  % of Raw│",
            "├─────────────────────────────────────────────────────┼──────────┼──────────┤",
            f"│ Registry Coverage                                   │ {cov_line:>20} │          │",
            f"│ Companies in list                                   │ {self.companies_in_list:>8} │          │",
            f"│ Companies scanned                                   │ {self.companies_scanned:>8} │          │",
            f"│ Companies reachable (HTTP 200)                      │ {self.companies_reachable:>8} │          │",
            f"│ Companies unreachable (HTTP 404/ERR)                │ {self.companies_unreachable:>8} │          │",
            f"│ Raw postings discovered                             │ {self.raw_postings:>8} │  100.0%  │",
            f"│ ✗ Date window dropped (>7d)                         │ {self.stale_date_dropped:>8} │ {pct(self.stale_date_dropped)}  │",
            f"│   Date unknown (proceeded)                          │ {self.date_unknown_processed:>8} │ {pct(self.date_unknown_processed)}  │",
            f"│ ✗ Non-US location dropped                           │ {self.location_non_us_dropped:>8} │ {pct(self.location_non_us_dropped)}  │",
            f"│ ✗ Dedupe: actioned (permanently suppressed)         │ {self.dedup_actioned_blocked:>8} │ {pct(self.dedup_actioned_blocked)}  │",
            f"│ ✗ Dedupe: stale rejected (<30d current gate)        │ {self.dedup_stale_rejected_blocked:>8} │ {pct(self.dedup_stale_rejected_blocked)}  │",
            f"│   Dedupe: re-admitted (older gate or >30d)          │ {self.re_admitted_count:>8} │ {pct(self.re_admitted_count)}  │",
            f"│ After dedupe & date filter                          │ {self.after_dedupe:>8} │ {pct(self.after_dedupe)}  │",
            "├─────────────────────────────────────────────────────┼──────────┼──────────┤",
            f"│ ✗ Sponsorship: explicit_no                          │ {self.sponsor_explicit_no:>8} │ {pct(self.sponsor_explicit_no)}  │",
            f"│   Sponsorship: confirmed                            │ {self.sponsor_confirmed:>8} │ {pct(self.sponsor_confirmed)}  │",
            f"│   Sponsorship: unknown (proceeds with flag)         │ {self.sponsor_unknown:>8} │ {pct(self.sponsor_unknown)}  │",
            f"│ After sponsorship gate                              │ {self.after_sponsorship:>8} │ {pct(self.after_sponsorship)}  │",
            "├─────────────────────────────────────────────────────┼──────────┼──────────┤",
            f"│ ✗ Exp: seniority title reject                       │ {self.exp_seniority_title:>8} │ {pct(self.exp_seniority_title)}  │",
            f"│ ✗ Exp: numeric exceeded (>3yr)                      │ {self.exp_exceeded:>8} │ {pct(self.exp_exceeded)}  │",
            f"│ ✗ Exp: unparseable (strict skip)                    │ {self.exp_unparseable:>8} │ {pct(self.exp_unparseable)}  │",
            f"│   Exp: unstated-pass (relaxation ON)                │ {self.exp_unstated_pass:>8} │ {pct(self.exp_unstated_pass)}  │",
            f"│ After experience gate                               │ {self.after_experience:>8} │ {pct(self.after_experience)}  │",
            "├─────────────────────────────────────────────────────┼──────────┼──────────┤",
            f"│ ✗ Rel: forbidden domain                             │ {self.rel_forbidden:>8} │ {pct(self.rel_forbidden)}  │",
            f"│ ✗ Rel: non-target role title                        │ {self.rel_non_target:>8} │ {pct(self.rel_non_target)}  │",
            f"│ ✗ Rel: low relevance (<0.60)                        │ {self.rel_low_relevance:>8} │ {pct(self.rel_low_relevance)}  │",
            f"│   Rel: title-match guarantee (relaxation ON)        │ {self.rel_title_guarantee:>8} │ {pct(self.rel_title_guarantee)}  │",
            f"│ After relevance gate                                │ {self.after_relevance:>8} │ {pct(self.after_relevance)}  │",
            f"│   ↳ Score dist 0.80-1.00                            │ {self.rel_dist_80_100:>8} │          │",
            f"│   ↳ Score dist 0.60-0.79 (PASS)                     │ {self.rel_dist_60_79:>8} │          │",
            f"│   ↳ Score dist 0.40-0.59 (NEAR MISS)                │ {self.rel_dist_40_59:>8} │          │",
            f"│   ↳ Score dist 0.20-0.39                            │ {self.rel_dist_20_39:>8} │          │",
            f"│   ↳ Score dist 0.00-0.19                            │ {self.rel_dist_00_19:>8} │          │",
            "├─────────────────────────────────────────────────────┼──────────┼──────────┤",
            f"│ ✗ Cap check: company cap blocked                    │ {self.cap_blocked:>8} │ {pct(self.cap_blocked)}  │",
            f"│ After company-cap check                             │ {self.cap_passed:>8} │ {pct(self.cap_passed)}  │",
            "├─────────────────────────────────────────────────────┼──────────┼──────────┤",
            f"│ ★ ELIGIBLE APPLICATIONS                             │ {self.eligible:>8} │ {pct(self.eligible)}  │",
            "└─────────────────────────────────────────────────────┴──────────┴──────────┘",
        ]
        return "\n".join(lines)


# Module-level funnel counter — reset per run
_current_funnel = FunnelCounters()

def get_current_funnel() -> FunnelCounters:
    """Return the current run's funnel counters."""
    return _current_funnel

def reset_funnel():
    """Reset funnel counters for a new run."""
    global _current_funnel
    _current_funnel = FunnelCounters()


# Unified Single Board Registry (Imported from authoritative app.services.company_registry)
from app.services.company_registry import (
    REAL_COMPANY_BOARDS,
    UNSUPPORTED_COMPANIES,
    format_coverage_line,
    get_coverage_summary,
    is_searchable,
    get_board_for_company,
    get_unsupported_reason,
)


EXCLUDED_KEYWORDS = [
    "senior", "sr.", "sr ", "staff", "principal", "lead", "director", "head of",
    "vp ", "manager", "architect", "5+ years", "6+ years", "7+ years", "8+ years",
    "10+ years", "4+ years", "5-7 years", "6-8 years", "7-10 years"
]


class JobScraper:
    """
    Multi-platform job scraper combining Greenhouse, Lever, and Ashby sources.
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

    def _check_sponsorship(self, description: str) -> str:
        """
        Explicit sponsorship gate.
        Returns: 'explicit_no', 'confirmed', or 'unknown'.
        """
        desc_lower = (description or "").lower()
        for pat in NO_SPONSORSHIP_PATTERNS:
            if re.search(pat, desc_lower):
                return "explicit_no"
        for pat in CONFIRMED_SPONSORSHIP_PATTERNS:
            if re.search(pat, desc_lower):
                return "confirmed"
        return "unknown"

    def _is_suitable(self, title: str, description: str) -> bool:
        """
        Full instrumented pipeline: Sponsorship → Experience → Relevance.
        Records counts into the module-level _current_funnel.
        """
        funnel = _current_funnel
        try:
            # ── Stage 1: Sponsorship gate (DISABLED per user direction 2026-09-25) ──
            # H-1B gate removed entirely; sponsorship language no longer blocks any posting.
            sponsor_result = self._check_sponsorship(description)
            if sponsor_result == "confirmed":
                funnel.sponsor_confirmed += 1
            else:
                funnel.sponsor_unknown += 1

            # ── Stage 2: Experience gate ──
            from app.services.experience_gate import ExperienceGate
            exp_ok, exp_reason, _ = ExperienceGate.evaluate(title, description)
            if not exp_ok:
                if "SENIORITY_EXCEEDED" in exp_reason:
                    funnel.exp_seniority_title += 1
                elif "UNPARSEABLE" in exp_reason:
                    funnel.exp_unparseable += 1
                else:
                    funnel.exp_exceeded += 1
                return False
            # Track unstated-pass separately
            if "UNSTATED_EXPERIENCE_PASS" in exp_reason:
                funnel.exp_unstated_pass += 1
            funnel.exp_pass += 1

            # ── Stage 3: Relevance gate ──
            from app.services.relevance_gate import RelevanceGate
            rel_ok, rel_reason, _ = RelevanceGate.evaluate(title, description)
            if not rel_ok:
                if "FORBIDDEN_DOMAIN" in rel_reason:
                    funnel.rel_forbidden += 1
                elif "NON_TARGET_ROLE" in rel_reason:
                    funnel.rel_non_target += 1
                else:
                    funnel.rel_low_relevance += 1
                return False
            if "TITLE_MATCH_GUARANTEE" in rel_reason:
                funnel.rel_title_guarantee += 1
            funnel.rel_pass += 1

            funnel.eligible += 1
            return True
        except Exception as e:
            logger.error(f"Pipeline error for '{title}': {e}")
            return False

    def _url_hash(self, url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()

    async def _fetch_greenhouse_raw(self, client: httpx.AsyncClient, company_name: str, handle: str) -> Optional[list[dict]]:
        """Fetch raw postings from Greenhouse API without filtering."""
        url = f"https://boards-api.greenhouse.io/v1/boards/{handle}/jobs?content=true"
        jobs = []
        try:
            from app.services.politeness import politeness
            await politeness.respect_rate_limit("boards-api.greenhouse.io")
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15.0)
            if r.status_code != 200:
                politeness.record_error("boards-api.greenhouse.io", r.status_code)
                return None
            politeness.record_success("boards-api.greenhouse.io")
            data = r.json()
            for item in data.get("jobs", []):
                title = item.get("title", "").strip()
                job_url = item.get("absolute_url", "").strip()
                if not title or not job_url:
                    continue
                location = item.get("location", {}).get("name", "Remote / Various")
                content = self._strip_html(item.get("content", ""))
                loc_lower = str(location).lower()
                title_lower = title.lower()
                remote_type = "Remote" if "remote" in loc_lower or "remote" in title_lower else ("Hybrid" if "hybrid" in loc_lower else "Onsite")
                updated_at = item.get("updated_at", "")
                if updated_at and len(updated_at) >= 10:
                    posted_date = updated_at[:10]
                    date_unknown = False
                else:
                    posted_date = None
                    date_unknown = True
                jobs.append({
                    "title": title, "company": company_name, "location": location,
                    "salary_min": None, "salary_max": None, "job_type": "Full-time",
                    "remote_type": remote_type,
                    "description": content[:3000] if content else f"Join {company_name} as a {title}.",
                    "requirements": f"Role at {company_name}. Location: {location}.",
                    "url": job_url, "source": f"{company_name} (Greenhouse)",
                    "posted_date": posted_date,
                    "date_unknown": date_unknown,
                })
            return jobs
        except Exception as e:
            logger.error(f"Greenhouse error for {company_name}: {e}")
            return None

    async def _fetch_lever_raw(self, client: httpx.AsyncClient, company_name: str, handle: str) -> Optional[list[dict]]:
        """Fetch raw postings from Lever API without filtering."""
        url = f"https://api.lever.co/v0/postings/{handle}?mode=json"
        jobs = []
        try:
            from app.services.politeness import politeness
            await politeness.respect_rate_limit("api.lever.co")
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15.0)
            if r.status_code != 200:
                politeness.record_error("api.lever.co", r.status_code)
                return None
            politeness.record_success("api.lever.co")
            for item in r.json():
                title = item.get("text", "").strip()
                job_url = item.get("hostedUrl", "").strip()
                if not title or not job_url:
                    continue
                categories = item.get("categories", {})
                location = categories.get("location", "Remote / Various")
                commitment = categories.get("commitment", "Full-time")
                desc = item.get("descriptionPlain", "")
                loc_lower = str(location).lower()
                remote_type = "Remote" if "remote" in loc_lower else ("Hybrid" if "hybrid" in loc_lower else "Onsite")
                created_at = item.get("createdAt")
                if created_at:
                    posted_date = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    date_unknown = False
                else:
                    posted_date = None
                    date_unknown = True
                jobs.append({
                    "title": title, "company": company_name, "location": location,
                    "salary_min": None, "salary_max": None, "job_type": commitment,
                    "remote_type": remote_type,
                    "description": desc[:3000] if desc else f"Join {company_name} as a {title}.",
                    "requirements": f"Role at {company_name}. Location: {location}.",
                    "url": job_url, "source": f"{company_name} (Lever)",
                    "posted_date": posted_date,
                    "date_unknown": date_unknown,
                })
            return jobs
        except Exception as e:
            logger.error(f"Lever error for {company_name}: {e}")
            return None

    async def _fetch_ashby_raw(self, client: httpx.AsyncClient, company_name: str, handle: str) -> Optional[list[dict]]:
        """Fetch raw postings from Ashby API without filtering."""
        url = f"https://api.ashbyhq.com/posting-api/job-board/{handle}"
        jobs = []
        try:
            from app.services.politeness import politeness
            await politeness.respect_rate_limit("api.ashbyhq.com")
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15.0)
            if r.status_code != 200:
                politeness.record_error("api.ashbyhq.com", r.status_code)
                return None
            politeness.record_success("api.ashbyhq.com")
            data = r.json()
            for item in data.get("jobs", []):
                title = item.get("title", "").strip()
                job_url = item.get("jobUrl", "") or item.get("applyUrl", "")
                if not title or not job_url:
                    continue
                location = item.get("location", "Remote / Various")
                desc_html = item.get("descriptionHtml", "")
                desc_plain = item.get("descriptionPlain", "") or self._strip_html(desc_html)
                loc_lower = str(location).lower()
                title_lower = title.lower()
                is_remote = item.get("isRemote", False)
                remote_type = "Remote" if is_remote or "remote" in loc_lower or "remote" in title_lower else ("Hybrid" if "hybrid" in loc_lower else "Onsite")
                pub_at = item.get("publishedAt", "")
                if pub_at and len(pub_at) >= 10:
                    posted_date = pub_at[:10]
                    date_unknown = False
                else:
                    posted_date = None
                    date_unknown = True
                jobs.append({
                    "title": title, "company": company_name, "location": location,
                    "salary_min": None, "salary_max": None, "job_type": item.get("employmentType", "Full-time"),
                    "remote_type": remote_type,
                    "description": desc_plain[:3000] if desc_plain else f"Join {company_name} as a {title}.",
                    "requirements": f"Role at {company_name}. Location: {location}.",
                    "url": job_url, "source": f"{company_name} (Ashby)",
                    "posted_date": posted_date,
                    "date_unknown": date_unknown,
                })
            return jobs
        except Exception as e:
            logger.error(f"Ashby error for {company_name}: {e}")
            return None

    # Backwards-compatible filtered fetch methods
    async def _fetch_greenhouse_jobs(self, client: httpx.AsyncClient, company_name: str, handle: str) -> list[dict]:
        raw = await self._fetch_greenhouse_raw(client, company_name, handle)
        return [j for j in (raw or []) if self._is_suitable(j["title"], j["description"])]

    async def _fetch_lever_jobs(self, client: httpx.AsyncClient, company_name: str, handle: str) -> list[dict]:
        raw = await self._fetch_lever_raw(client, company_name, handle)
        return [j for j in (raw or []) if self._is_suitable(j["title"], j["description"])]

    async def _fetch_ashby_jobs(self, client: httpx.AsyncClient, company_name: str, handle: str) -> list[dict]:
        raw = await self._fetch_ashby_raw(client, company_name, handle)
        return [j for j in (raw or []) if self._is_suitable(j["title"], j["description"])]

    # Sourcing path retired: Indeed/JobSpy completely deleted per prompt B.1.
    # The ATS registry (Greenhouse, Lever, Ashby) is now the only sourcing path.

    # Compatibility alias
    def _is_suitable_experience(self, title: str, description: str) -> bool:
        return self._is_suitable(title, description)

    async def search_jobs(self, filters: list[JobFilter], max_companies: Optional[int] = None, lookback_days: int = 7, **kwargs) -> list[dict]:
        """
        Fetch from direct company career boards (Greenhouse, Lever, Ashby).
        Instruments counts at EVERY stage:
          companies scanned → raw postings → dedupe → sponsorship split →
          experience split → relevance distribution → cap check → eligible.
        """
        all_jobs = []
        seen_urls = set()

        reset_funnel()
        funnel = get_current_funnel()
        cov_s, cov_u, cov_tot = get_coverage_summary()
        funnel.coverage_searchable = cov_s
        funnel.coverage_unsupported = cov_u
        funnel.coverage_total = cov_tot

        target_boards = REAL_COMPANY_BOARDS[:max_companies] if max_companies else REAL_COMPANY_BOARDS
        funnel.companies_in_list = len(target_boards)

        search_keywords = []
        for f in filters:
            if f.role_keywords:
                search_keywords.extend([kw.strip().lower() for kw in f.role_keywords.split(",") if kw.strip()])

        # Direct Company Career Boards
        async with httpx.AsyncClient(follow_redirects=True) as client:
            for company_name, board_type, handle in target_boards:
                funnel.companies_scanned += 1
                logger.info(f"Scanning company ({funnel.companies_scanned}/{len(target_boards)}): {company_name} ({board_type}/{handle})")

                if board_type == "greenhouse":
                    company_jobs = await self._fetch_greenhouse_raw(client, company_name, handle)
                elif board_type == "lever":
                    company_jobs = await self._fetch_lever_raw(client, company_name, handle)
                elif board_type == "ashby":
                    company_jobs = await self._fetch_ashby_raw(client, company_name, handle)
                else:
                    company_jobs = None

                if company_jobs is None:
                    funnel.companies_unreachable += 1
                    funnel.unreachable_names.append(f"{company_name} ({board_type}/{handle})")
                    continue

                funnel.companies_reachable += 1

                for j in company_jobs:
                    funnel.raw_postings += 1
                    title = j["title"]
                    desc = j["description"]
                    url = j["url"]
                    location = j.get("location", "")
                    posted_date = j.get("posted_date")

                    # ── Stage 1A: In-memory session dedupe ──
                    if url in seen_urls:
                        funnel.dedupe_removed += 1
                        continue
                    seen_urls.add(url)

                    # ── Stage 1B: Date window lookback filter ──
                    if posted_date:
                        try:
                            post_dt = datetime.strptime(posted_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            age_hours = (datetime.now(timezone.utc) - post_dt).total_seconds() / 3600.0
                            if age_hours > (lookback_days * 24):
                                funnel.stale_date_dropped += 1
                                continue
                        except Exception:
                            funnel.date_unknown_processed += 1
                    else:
                        funnel.date_unknown_processed += 1

                    # ── Stage 1C: US-only Location Gate ──
                    from app.services.location_gate import is_us_location
                    if not is_us_location(location, title, desc):
                        funnel.location_non_us_dropped += 1
                        continue

                    # ── Stage 1D: Dedup Gate (jobs_seen.db: evaluated vs actioned) ──
                    from app.services.dedup_gate import DedupGate
                    dedup_status, dedup_reason = DedupGate.evaluate_job(company_name, url)
                    if dedup_status == "actioned":
                        funnel.dedup_actioned_blocked += 1
                        continue
                    elif dedup_status == "stale_rejected":
                        funnel.dedup_stale_rejected_blocked += 1
                        continue
                    elif dedup_status == "re_admitted":
                        funnel.re_admitted_count += 1

                    funnel.after_dedupe += 1

                    # ── Stage 2: Sponsorship Gate (DISABLED per user direction 2026-09-25) ──
                    # H-1B gate removed entirely; sponsorship language no longer blocks.
                    s_status = self._check_sponsorship(desc)
                    if s_status == "confirmed":
                        funnel.sponsor_confirmed += 1
                    else:
                        funnel.sponsor_unknown += 1
                    funnel.after_sponsorship += 1

                    # ── Stage 3: Experience Gate ──
                    from app.services.experience_gate import ExperienceGate
                    exp_ok, exp_reason, _ = ExperienceGate.evaluate(title, desc)
                    if not exp_ok:
                        if "SENIORITY_EXCEEDED" in exp_reason:
                            funnel.exp_seniority_title += 1
                        elif "UNPARSEABLE" in exp_reason:
                            funnel.exp_unparseable += 1
                        else:
                            funnel.exp_exceeded += 1
                        DedupGate.mark_evaluated(company_name, url, title, outcome="rejected", reason=f"experience_{exp_reason}")
                        continue
                    if "UNSTATED_EXPERIENCE_PASS" in exp_reason:
                        funnel.exp_unstated_pass += 1
                    funnel.exp_pass += 1
                    funnel.after_experience += 1

                    # ── Stage 4: Relevance Gate ──
                    from app.services.relevance_gate import RelevanceGate
                    rel_ok, rel_reason, rel_meta = RelevanceGate.evaluate(title, desc)
                    rel_score = rel_meta.get("relevance_score", 0.0)
                    funnel.record_relevance_score(rel_score)
                    if not rel_ok:
                        if "FORBIDDEN_DOMAIN" in rel_reason:
                            funnel.rel_forbidden += 1
                        elif "NON_TARGET_ROLE" in rel_reason:
                            funnel.rel_non_target += 1
                        else:
                            funnel.rel_low_relevance += 1
                        DedupGate.mark_evaluated(company_name, url, title, outcome="rejected", reason=f"relevance_{rel_reason}")
                        continue
                    if "TITLE_MATCH_GUARANTEE" in rel_reason:
                        funnel.rel_title_guarantee += 1
                    funnel.rel_pass += 1
                    funnel.after_relevance += 1

                    # ── Stage 5: Company-Cap Check ──
                    try:
                        from app.services.company_cap import company_cap_tracker
                        is_capped, _, _ = company_cap_tracker.is_company_capped(company_name, url)
                    except Exception:
                        is_capped = False
                    if is_capped:
                        funnel.cap_blocked += 1
                        DedupGate.mark_evaluated(company_name, url, title, outcome="skipped", reason="company_cap_reached")
                        continue
                    funnel.cap_passed += 1

                    # Optional keyword filter match (if filters set)
                    if search_keywords:
                        text = f"{title} {desc}".lower()
                        if not any(kw in text for kw in search_keywords):
                            continue

                    # ── Final: Eligible ──
                    funnel.eligible += 1
                    all_jobs.append(j)

        # Note: Broad third-party sourcing retired per prompt B.1. ATS registry is the sole source.
        logger.info(f"\n{funnel.to_table()}")
        return all_jobs

    async def scrape_and_save(self, db: AsyncSession, filters: list[JobFilter], max_companies: Optional[int] = None, lookback_days: int = 7, **kwargs) -> int:
        jobs_data = await self.search_jobs(filters, max_companies=max_companies, lookback_days=lookback_days, **kwargs)
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

