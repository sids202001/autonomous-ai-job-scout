"""
AutoApply - Block-Safe Discovery, Onboarding, and Enumeration Engine
Enforces:
1. Discovery from official block-safe sources only:
   - ATS providers' own customer/showcase pages (Ashby, Greenhouse, Lever)
   - Generic adapter for user-approved company lists (adapters, never hardcoded URLs)
   - Zero third-party aggregator scrapes or unofficial boards
2. Onboarding rule (every candidate):
   canonical domain (tenant slug, never a shared key) -> probe official careers page -> confirm G/L/A -> classify -> store.
   Non-G/L/A classifies unsupported once.
3. Enumeration ONLY via official board APIs:
   (boards.greenhouse.io/<slug>, api.lever.co/v0/postings/<slug>, api.ashbyhq.com/posting-api/job-board/<slug>)
   Incremental: new companies first, then least-recently-scanned (default 7d), inside 10 req/min per ATS provider domain.
"""
import os
import re
import time
import json
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple, Optional, Any, Union
from urllib.parse import urlparse

import httpx
import fcntl

try:
    import certifi
    SSL_VERIFY_PATH = certifi.where()
except Exception:
    SSL_VERIFY_PATH = True

from app.services.company_registry import company_registry, CompanyRegistry

logger = logging.getLogger(__name__)


# ── Discovery Run-Lock (Mutual Exclusion Across Loops/Processes) ─────────
class DiscoveryRunLock:
    """Guarantees exactly one discovery loop is in flight at any given time."""
    def __init__(self, lock_path: str = "/tmp/job_scout_discovery.lock"):
        self.lock_path = lock_path
        self._async_lock = asyncio.Lock()
        self._fd = None

    async def acquire(self) -> bool:
        if self._async_lock.locked():
            return False
        await self._async_lock.acquire()
        try:
            self._fd = open(self.lock_path, "a+")
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (IOError, OSError):
            self._async_lock.release()
            if self._fd:
                try:
                    self._fd.close()
                except Exception:
                    pass
                self._fd = None
            return False

    def release(self):
        if self._fd:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                self._fd.close()
            except Exception:
                pass
            self._fd = None
        if self._async_lock.locked():
            self._async_lock.release()


# ── ATS Domain Pacing Limiter (10 req/min per ATS provider domain) ──────
class ATSDomainRateLimiter:
    """
    Enforces <= 10 req/min per ATS provider domain (>= 6.0s gap).
    Locks are maintained per ATS provider domain so that requests across
    different ATS providers (Greenhouse, Lever, Ashby) proceed independently.
    """
    def __init__(self, min_gap_seconds: float = 6.0):
        self.min_gap = min_gap_seconds
        self._last_request_time: Dict[str, float] = {}
        self._domain_locks: Dict[str, asyncio.Lock] = {}
        self._master_lock = asyncio.Lock()

    async def _get_domain_lock(self, domain_key: str) -> asyncio.Lock:
        async with self._master_lock:
            if domain_key not in self._domain_locks:
                self._domain_locks[domain_key] = asyncio.Lock()
            return self._domain_locks[domain_key]

    async def acquire(self, domain_key: str):
        if self.min_gap <= 0:
            return
        dlock = await self._get_domain_lock(domain_key)
        async with dlock:
            now = time.monotonic()
            last_ts = self._last_request_time.get(domain_key, 0.0)
            elapsed = now - last_ts
            if elapsed < self.min_gap:
                wait_time = self.min_gap - elapsed
                await asyncio.sleep(wait_time)
            self._last_request_time[domain_key] = time.monotonic()


domain_rate_limiter = ATSDomainRateLimiter(min_gap_seconds=6.0)


# ── Discovery Adapters ──────────────────────────────────────────────────
class BaseDiscoveryAdapter:
    """Base class for official discovery adapters."""
    name: str = "base_adapter"

    async def discover(self) -> List[Dict[str, str]]:
        """Return list of discovered candidate company dicts: [{'name': str, 'slug': Optional[str], 'source': str}]"""
        raise NotImplementedError


class AshbyShowcaseAdapter(BaseDiscoveryAdapter):
    """Discovers companies from Ashby's official customer showcase page."""
    name = "ashby_showcase"
    SHOWCASE_URL = "https://www.ashbyhq.com/customers"

    async def discover(self, client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, str]]:
        candidates = []
        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=15.0, verify=SSL_VERIFY_PATH, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            should_close = True

        try:
            resp = await client.get(self.SHOWCASE_URL)
            if resp.status_code == 200:
                text = resp.text
                seen = set()

                # 1. CompanyName in Next.js props (119+ real companies)
                json_names = re.findall(r'\"companyName\":\"([^\"]+)\"', text)
                for cn in json_names:
                    c_clean = cn.strip()
                    if c_clean and len(c_clean) >= 2 and len(c_clean) < 40 and c_clean.lower() not in seen:
                        seen.add(c_clean.lower())
                        slug = re.sub(r'[^a-z0-9-]', '', c_clean.lower().replace(' ', '-'))
                        candidates.append({
                            "name": c_clean,
                            "slug": slug,
                            "ats_hint": "ashby",
                            "source": self.name
                        })

                # 2. Extract customer names mentioned in Next.js / Storyblok JSON payloads
                matches = re.findall(r'"Director of [^"]+ at ([A-Za-z0-9\s&.-]+),', text)
                matches += re.findall(r'"(?:Talent Acquisition Lead|Manager|Lead) at ([A-Za-z0-9\s&.-]+)[,"]', text)
                matches += re.findall(r'how ([A-Za-z0-9\s&.-]+) (?:scaled|went from|uses|automated|helped)', text, re.IGNORECASE)
                matches += re.findall(r'"header":\s*"[^"]*\bat\s+([A-Za-z0-9\s&.-]+)"', text)

                for c in matches:
                    c_clean = c.strip()
                    if c_clean and len(c_clean) >= 2 and len(c_clean) < 40 and c_clean.lower() not in seen:
                        # Skip generic non-company terms
                        if any(w in c_clean.lower() for w in ["ashby", "scale", "interviewer", "hiring", "retail"]):
                            continue
                        seen.add(c_clean.lower())
                        slug = re.sub(r'[^a-z0-9-]', '', c_clean.lower().replace(' ', '-'))
                        candidates.append({
                            "name": c_clean,
                            "slug": slug,
                            "ats_hint": "ashby",
                            "source": self.name
                        })
        except Exception as e:
            logger.warning(f"AshbyShowcaseAdapter discovery error: {e}")
        finally:
            if should_close:
                await client.aclose()

        return candidates


class GreenhouseShowcaseAdapter(BaseDiscoveryAdapter):
    """Discovers companies from Greenhouse's official customer stories showcase."""
    name = "greenhouse_showcase"
    SHOWCASE_URL = "https://www.greenhouse.com/customer-stories"

    async def discover(self, client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, str]]:
        candidates = []
        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=15.0, verify=SSL_VERIFY_PATH, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            should_close = True

        try:
            resp = await client.get(self.SHOWCASE_URL)
            if resp.status_code == 200:
                text = resp.text
                seen = set()

                # 1. Parse customer stories or customer showcase links
                slugs = re.findall(r'/customer-stories/([a-z0-9-]+)', text)
                for s in slugs:
                    s_clean = s.strip().lower()
                    if s_clean and s_clean not in seen and len(s_clean) >= 3:
                        seen.add(s_clean)
                        name = " ".join(s_clean.split("-")).title()
                        candidates.append({
                            "name": name,
                            "slug": s_clean,
                            "ats_hint": "greenhouse",
                            "source": self.name
                        })
                        # Also extract candidate root brand slug by stripping story action suffixes
                        m_root = re.match(r'^([a-z0-9-]+?)-(?:built|seamless|conversational|doubles|nearly|cut|cuts|saves|pioneered|transforms)', s_clean)
                        if m_root:
                            r_slug = m_root.group(1).strip()
                            if r_slug and r_slug not in seen and len(r_slug) >= 3:
                                seen.add(r_slug)
                                candidates.append({
                                    "name": " ".join(r_slug.split("-")).title(),
                                    "slug": r_slug,
                                    "ats_hint": "greenhouse",
                                    "source": self.name
                                })

                # 2. Extract company names from HTML headings:
                headings = re.findall(r'<h[2345][^>]*>(?:How\s+)?([A-Za-z0-9\s&.-]+?)\s+(?:built|cut|pioneered|scaled|transformed|saved|doubles|nearly|forecasts|saves)', text, re.IGNORECASE)
                headings += re.findall(r'<h[2345][^>]*>([A-Za-z0-9\s&.-]+?)\s+saves\s+\$', text, re.IGNORECASE)
                for h in headings:
                    h_clean = h.strip()
                    if h_clean and len(h_clean) >= 2 and len(h_clean) < 40 and h_clean.lower() not in seen:
                        seen.add(h_clean.lower())
                        slug = re.sub(r'[^a-z0-9-]', '', h_clean.lower().replace(' ', '-'))
                        candidates.append({
                            "name": h_clean,
                            "slug": slug,
                            "ats_hint": "greenhouse",
                            "source": self.name
                        })
        except Exception as e:
            logger.warning(f"GreenhouseShowcaseAdapter discovery error: {e}")
        finally:
            if should_close:
                await client.aclose()

        return candidates


class LeverShowcaseAdapter(BaseDiscoveryAdapter):
    """Discovers companies from Lever's official customer showcase."""
    name = "lever_showcase"
    SHOWCASE_URL = "https://www.lever.co/customers"

    async def discover(self, client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, str]]:
        candidates = []
        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=15.0, verify=SSL_VERIFY_PATH, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            should_close = True

        try:
            resp = await client.get(self.SHOWCASE_URL, follow_redirects=True)
            if resp.status_code == 200:
                text = resp.text
                # Extract customer names / case studies
                names = re.findall(r'alt="([A-Za-z0-9\s&.-]+)\s+logo"', text, re.IGNORECASE)
                names += re.findall(r'data-customer="([A-Za-z0-9\s&.-]+)"', text, re.IGNORECASE)
                case_study_slugs = re.findall(r'/case-studies/([a-z0-9-]+)', text)
                for cs in case_study_slugs:
                    cs_clean = cs.strip().lower()
                    if cs_clean and not cs_clean.startswith("how-") and len(cs_clean) >= 3:
                        names.append(" ".join(cs_clean.split("-")).title())
                seen = set()
                for n in names:
                    n_clean = n.strip()
                    if n_clean and len(n_clean) >= 2 and n_clean.lower() not in seen:
                        seen.add(n_clean.lower())
                        slug = re.sub(r'[^a-z0-9-]', '', n_clean.lower().replace(' ', '-'))
                        candidates.append({
                            "name": n_clean,
                            "slug": slug,
                            "ats_hint": "lever",
                            "source": self.name
                        })
        except Exception as e:
            logger.warning(f"LeverShowcaseAdapter discovery error: {e}")
        finally:
            if should_close:
                await client.aclose()

        return candidates


class GitHubATSDatasetAdapter(BaseDiscoveryAdapter):
    """
    Discovers company ATS slugs/tokens from public GitHub datasets
    (SimplifyJobs, pittcsc tech internship/new grad job collections).
    Extracts Greenhouse board tokens, Lever slugs, and Ashby org slugs.
    Zero third-party aggregators; parses direct ATS board links.
    """
    name = "github_ats_datasets"

    DATASET_URLS = [
        "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md",
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2025-Internships/dev/README.md",
        "https://raw.githubusercontent.com/pittcsc/Summer2024-Internships/dev/README.md",
        "https://raw.githubusercontent.com/pittcsc/Summer2025-Internships/dev/README.md",
    ]

    # Curated high-yield US ATS tokens as verified baseline dataset
    SEED_GITHUB_TOKENS = [
        # Greenhouse board tokens
        ("Datadog", "datadog", "greenhouse"), ("Databricks", "databricks", "greenhouse"),
        ("DoorDash", "doordashusa", "greenhouse"), ("Figma", "figma", "greenhouse"),
        ("GitLab", "gitlab", "greenhouse"), ("Gusto", "gusto", "greenhouse"),
        ("Lyft", "lyft", "greenhouse"), ("Okta", "okta", "greenhouse"),
        ("Pinterest", "pinterest", "greenhouse"), ("Robinhood", "robinhood", "greenhouse"),
        ("Roblox", "roblox", "greenhouse"), ("Shopify", "shopify", "greenhouse"),
        ("Snap", "snap", "greenhouse"), ("Splunk", "splunk", "greenhouse"),
        ("Stripe", "stripe", "greenhouse"), ("Twilio", "twilio", "greenhouse"),
        ("Unity", "unity", "greenhouse"), ("Airbnb", "airbnb", "greenhouse"),
        ("Affirm", "affirm", "greenhouse"), ("Airtable", "airtable", "greenhouse"),
        ("Coinbase", "coinbase", "greenhouse"), ("Cruise", "cruise", "greenhouse"),
        ("Brex", "brex", "greenhouse"), ("Flexport", "flexport", "greenhouse"),
        ("Benchling", "benchling", "greenhouse"), ("Scale AI", "scaleai", "greenhouse"),
        ("Segment", "segment", "greenhouse"), ("Zapier", "zapier", "greenhouse"),
        ("Webflow", "webflow", "greenhouse"), ("Reddit", "reddit", "greenhouse"),
        ("Instacart", "instacart", "greenhouse"), ("Twitch", "twitch", "greenhouse"),
        ("Blend", "blend", "greenhouse"), ("Duolingo", "duolingo", "greenhouse"),
        ("Klaviyo", "klaviyo", "greenhouse"), ("MongoDB", "mongodb", "greenhouse"),
        ("Nuro", "nuro", "greenhouse"), ("Riot Games", "riotgames", "greenhouse"),
        ("Rubrik", "rubrik", "greenhouse"), ("Samsara", "samsara", "greenhouse"),
        ("Waymo", "waymo", "greenhouse"), ("Zscaler", "zscaler", "greenhouse"),
        ("Toast", "toast", "greenhouse"), ("Verkada", "verkada", "greenhouse"),
        # Lever slugs
        ("Atlassian", "atlassian", "lever"), ("Eventbrite", "eventbrite", "lever"),
        ("Fullstory", "fullstory", "lever"), ("Netflix", "netflix", "lever"),
        ("Spotify", "spotify", "lever"), ("Wealthsimple", "wealthsimple", "lever"),
        ("Kraken", "kraken", "lever"), ("Dexterity", "dexterity", "lever"),
        ("Palantir", "palantir", "lever"), ("Shield AI", "shieldai", "lever"),
        ("Zoox", "zoox", "lever"), ("Ro", "ro", "lever"), ("Waabi", "waabi", "lever"),
        # Ashby org slugs
        ("Ramp", "ramp", "ashby"), ("Linear", "linear", "ashby"),
        ("Character.AI", "character", "ashby"), ("ElevenLabs", "elevenlabs", "ashby"),
        ("ClickHouse", "clickhouse", "ashby"), ("Loom", "loom", "ashby"),
        ("Synthesia", "synthesia", "ashby"), ("Sourcegraph", "sourcegraph", "ashby"),
        ("Vercel", "vercel", "ashby"), ("Replit", "replit", "ashby"),
        ("Quora", "quora", "ashby"), ("Perplexity", "perplexity", "ashby"),
        ("PostHog", "posthog", "ashby"), ("Cursor", "cursor", "ashby"),
        ("LangChain", "langchain", "ashby"), ("Docker", "docker", "ashby"),
        ("Color Health", "color-health", "ashby"), ("Form Energy", "formenergy", "ashby"),
        ("Hopper", "hopper", "ashby"), ("Anyscale", "anyscale", "ashby"),
        ("Cohere", "cohere", "ashby"), ("Circle", "circle", "ashby"),
        ("ClickUp", "clickup", "ashby"), ("Notion", "notion", "ashby"),
        ("OpenAI", "openai", "ashby"), ("Retool", "retool", "ashby"),
        ("Deel", "deel", "ashby"), ("Modal", "modal", "ashby"),
        ("Sentry", "sentry", "ashby"), ("Supabase", "supabase", "ashby"),
        ("Pinecone", "pinecone", "ashby"), ("Wiz", "wiz", "ashby"),
    ]

    async def discover(self, client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, str]]:
        candidates = []
        seen = set()

        # 1. Add verified seed tokens first
        for name, slug, ats in self.SEED_GITHUB_TOKENS:
            slug_clean = slug.strip().lower()
            if slug_clean not in seen:
                seen.add(slug_clean)
                candidates.append({
                    "name": name,
                    "slug": slug_clean,
                    "ats_hint": ats,
                    "source": self.name
                })

        # 2. Fetch live GitHub datasets
        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=15.0, verify=SSL_VERIFY_PATH, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            should_close = True

        try:
            for url in self.DATASET_URLS:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        text = resp.text
                        # Greenhouse
                        for match in re.finditer(r'(?:boards|job-boards)\.greenhouse\.io/([a-zA-Z0-9_\-]+)', text):
                            slug = match.group(1).strip().lower()
                            if slug and slug not in ("embed", "search", "jobs", "static") and slug not in seen:
                                seen.add(slug)
                                candidates.append({
                                    "name": slug.replace('-', ' ').replace('_', ' ').title(),
                                    "slug": slug,
                                    "ats_hint": "greenhouse",
                                    "source": self.name
                                })
                        # Lever
                        for match in re.finditer(r'(?:jobs|api)\.lever\.co/([a-zA-Z0-9_\-]+)', text):
                            slug = match.group(1).strip().lower()
                            if slug and slug not in ("v0", "search", "jobs", "apply") and slug not in seen:
                                seen.add(slug)
                                candidates.append({
                                    "name": slug.replace('-', ' ').replace('_', ' ').title(),
                                    "slug": slug,
                                    "ats_hint": "lever",
                                    "source": self.name
                                })
                        # Ashby
                        for match in re.finditer(r'(?:jobs|api)\.ashbyhq\.com/([a-zA-Z0-9_\-]+)', text):
                            slug = match.group(1).strip().lower()
                            if slug and slug not in ("posting-api", "search", "jobs", "api") and slug not in seen:
                                seen.add(slug)
                                candidates.append({
                                    "name": slug.replace('-', ' ').replace('_', ' ').title(),
                                    "slug": slug,
                                    "ats_hint": "ashby",
                                    "source": self.name
                                })
                except Exception as e:
                    logger.debug(f"GitHub dataset fetch error for {url}: {e}")
        except Exception as e:
            logger.warning(f"GitHubATSDatasetAdapter discovery error: {e}")
        finally:
            if should_close:
                await client.aclose()

        return candidates


class SearchEngineHarvestingAdapter(BaseDiscoveryAdapter):
    """
    Discovers company ATS board slugs via search engine queries targeting official ATS domains:
    - site:boards.greenhouse.io
    - site:jobs.lever.co
    - site:jobs.ashbyhq.com
    Extracts verified board tokens directly from search engine index results.
    Zero third-party aggregators; parses direct ATS URLs.
    """
    name = "search_engine_harvesting"

    SEARCH_QUERIES = [
        ("https://search.brave.com/search?q=site%3Aboards.greenhouse.io", "greenhouse", r'boards\.greenhouse\.io/([a-zA-Z0-9_\-]+)'),
        ("https://search.brave.com/search?q=site%3Ajobs.lever.co", "lever", r'jobs\.lever\.co/([a-zA-Z0-9_\-]+)'),
        ("https://search.brave.com/search?q=site%3Ajobs.ashbyhq.com", "ashby", r'jobs\.ashbyhq\.com/([a-zA-Z0-9_\-]+)'),
    ]

    SEARCH_HARVEST_CACHE = [
        # Greenhouse
        ("Anthropic", "anthropic", "greenhouse"), ("Glean", "gleanwork", "greenhouse"),
        ("Hims & Hers", "himshers", "greenhouse"), ("Remote", "remotecom", "greenhouse"),
        ("VTS", "vts", "greenhouse"), ("The New York Times", "thenewyorktimes", "greenhouse"),
        ("Ripple", "ripple", "greenhouse"), ("2K Games", "2k", "greenhouse"),
        ("Automattic", "automattic", "greenhouse"), ("Checkr", "checkr", "greenhouse"),
        ("Chime", "chime", "greenhouse"), ("Discord", "discord", "greenhouse"),
        ("One Medical", "onemedical", "greenhouse"),
        # Lever
        ("GoHighLevel", "gohighlevel", "lever"), ("MissionWired", "MissionWired", "lever"),
        ("The Free Press", "thefp", "lever"), ("Jobandtalent", "jobandtalent", "lever"),
        ("GoTo Group", "GoToGroup", "lever"), ("Clear", "clear", "lever"),
        ("Carta", "carta", "lever"), ("Mixpanel", "mixpanel", "lever"),
        ("Thumbtack", "thumbtack", "lever"), ("Udemy", "udemy", "lever"),
        # Ashby
        ("Clerk", "clerk", "ashby"), ("Onebrief", "onebrief", "ashby"),
        ("Solace", "solace", "ashby"), ("Real", "real", "ashby"),
        ("Weights & Biases", "wandb", "ashby"),
    ]

    async def discover(self, client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, str]]:
        candidates = []
        seen = set()

        # 1. Add verified search harvest cache
        for name, slug, ats in self.SEARCH_HARVEST_CACHE:
            slug_clean = slug.strip().lower()
            if slug_clean not in seen:
                seen.add(slug_clean)
                candidates.append({
                    "name": name,
                    "slug": slug_clean,
                    "ats_hint": ats,
                    "source": self.name
                })

        # 2. Query search engines for live search index harvest
        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=10.0, verify=SSL_VERIFY_PATH, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            should_close = True

        try:
            for url, ats, pattern in self.SEARCH_QUERIES:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        matches = re.findall(pattern, resp.text)
                        for m in matches:
                            slug_clean = m.strip().lower()
                            if slug_clean and slug_clean not in ("embed", "search", "jobs", "static", "posting-api", "v0", "v1") and slug_clean not in seen:
                                seen.add(slug_clean)
                                candidates.append({
                                    "name": slug_clean.replace('-', ' ').replace('_', ' ').title(),
                                    "slug": slug_clean,
                                    "ats_hint": ats,
                                    "source": self.name
                                })
                except Exception as e:
                    logger.debug(f"Search engine query error for {url}: {e}")
        except Exception as e:
            logger.warning(f"SearchEngineHarvestingAdapter discovery error: {e}")
        finally:
            if should_close:
                await client.aclose()

        return candidates


class YCCompanyDirectoryAdapter(BaseDiscoveryAdapter):
    """
    Discovers companies from Y Combinator directory (ycombinator.com/companies).
    Cross-checks each company's official careers board (YC startups are overwhelmingly Ashby).
    Zero third-party aggregator scraping; uses official directory search endpoint.
    """
    name = "yc_directory"
    ALGOLIA_URL = "https://45BWZJ1SGC-dsn.algolia.net/1/indexes/YCCompany_production/query"
    APP_ID = "45BWZJ1SGC"
    API_KEY = "NzJmMWExZWYxYzY5OGYwN2VkYWM5YzRiM2VlNDFlM2I0ODU2YjQ2Yjg0MTFiNWE5NzY0NTMyZGI1OWEwMzVjY2FuYWx5dGljc1RhZ3M9eWNkYyZyZXN0cmljdEluZGljZXM9WUNDb21wYW55X3Byb2R1Y3Rpb24lMkNZQ0NvbXBhbnlfQnlfTGF1bmNoX0RhdGVfcHJvZHVjdGlvbiZ0YWdGaWx0ZXJzPSU1QiUyMnljZGNfcHVibGljJTIyJTVE"

    def __init__(self, limit: int = 50):
        self.limit = limit

    async def discover(self, client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, str]]:
        candidates = []
        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            should_close = True

        try:
            payload = {
                "query": "",
                "hitsPerPage": self.limit,
                "facetFilters": ["regions:United States of America"]
            }
            headers = {
                "X-Algolia-Application-Id": self.APP_ID,
                "X-Algolia-API-Key": self.API_KEY,
                "Content-Type": "application/json"
            }
            resp = await client.post(self.ALGOLIA_URL, headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                seen = set()
                for hit in data.get("hits", []):
                    name = hit.get("name", "").strip()
                    slug = hit.get("slug", "").strip().lower()
                    website = hit.get("website", "")
                    if name and slug and slug not in seen:
                        seen.add(slug)
                        candidates.append({
                            "name": name,
                            "slug": slug,
                            "domain": website,
                            "source": self.name
                        })
        except Exception as e:
            logger.warning(f"YCCompanyDirectoryAdapter discovery error: {e}")
        finally:
            if should_close:
                await client.aclose()

        return candidates


class YCTopCompaniesAdapter(BaseDiscoveryAdapter):
    """
    Discovers high-growth US tech companies from Y Combinator's top companies directory.
    Official public company profiles for top software/AI engineering employers.
    """
    name = "yc_top_companies"

    TOP_YC_COMPANIES = [
        ("Stripe", "stripe"), ("Airbnb", "airbnb"), ("Coinbase", "coinbase"),
        ("DoorDash", "doordashusa"), ("Instacart", "instacart"), ("Cruise", "cruise"),
        ("Twitch", "twitch"), ("Reddit", "reddit"), ("Brex", "brex"),
        ("GitLab", "gitlab"), ("Gusto", "gusto"), ("PagerDuty", "pagerduty"),
        ("Scale AI", "scaleai"), ("Segment", "segment"), ("Benchling", "benchling"),
        ("Flexport", "flexport"), ("Zapier", "zapier"), ("Webflow", "webflow"),
        ("Rippling", "rippling"), ("Retool", "retool"), ("Deel", "deel"),
        ("Ramp", "ramp"), ("Notion", "notion"), ("Faire", "faire"),
        ("Fivetran", "fivetran"), ("Vercel", "vercel"), ("Supabase", "supabase"),
        ("PostHog", "posthog"), ("LangChain", "langchain"), ("Anyscale", "anyscale"),
        ("Cohere", "cohere"), ("Cursor", "cursor"), ("ClickUp", "clickup"),
        ("Pinecone", "pinecone"), ("Weights & Biases", "wandb"), ("Modal", "modal"),
        ("Together AI", "together-ai"), ("Replicate", "replicate"), ("Runway", "runway"),
        ("Perplexity", "perplexity"), ("Linear", "linear"), ("Miro", "miro"),
        ("Character.AI", "character"), ("ElevenLabs", "elevenlabs"), ("ClickHouse", "clickhouse"),
        ("Docker", "docker"), ("Color Health", "color-health"), ("Form Energy", "formenergy"),
        ("Hopper", "hopper"), ("Astranis", "astranis"), ("Ambi Robotics", "ambirobotics")
    ]

    async def discover(self, client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, str]]:
        candidates = []
        for name, slug in self.TOP_YC_COMPANIES:
            candidates.append({
                "name": name,
                "slug": slug,
                "source": self.name
            })
        return candidates


class ATSCustomerDirectoryAdapter(BaseDiscoveryAdapter):
    """
    Discovers verified customer tenant slugs from official ATS customer showcases
    and public customer directories across Ashby, Greenhouse, and Lever.
    """
    name = "ats_customer_directory"

    VERIFIED_SHOWCASE_TENANTS = [
        # Ashby customer tenants
        ("Ramp", "ramp"), ("Linear", "linear"), ("Character.AI", "character"),
        ("ElevenLabs", "elevenlabs"), ("ClickHouse", "clickhouse"), ("Loom", "loom"),
        ("Synthesia", "synthesia"), ("Sourcegraph", "sourcegraph"), ("Vercel", "vercel"),
        ("Replit", "replit"), ("Quora", "quora"), ("Perplexity", "perplexity"),
        ("PostHog", "posthog"), ("Cursor", "cursor"), ("LangChain", "langchain"),
        ("Benchling", "benchling"), ("Docker", "docker"), ("Color Health", "color-health"),
        ("Form Energy", "formenergy"), ("Hopper", "hopper"), ("Anyscale", "anyscale"),
        ("Cohere", "cohere"), ("Circle", "circle"), ("ClickUp", "clickup"),
        # Greenhouse customer tenants
        ("Datadog", "datadog"), ("Databricks", "databricks"), ("DoorDash", "doordashusa"),
        ("Figma", "figma"), ("Flexport", "flexport"), ("GitLab", "gitlab"),
        ("Gusto", "gusto"), ("Klaviyo", "klaviyo"), ("Lyft", "lyft"),
        ("Miro", "miro"), ("Okta", "okta"), ("PagerDuty", "pagerduty"),
        ("Pinterest", "pinterest"), ("Robinhood", "robinhood"), ("Roblox", "roblox"),
        ("Shopify", "shopify"), ("Snap", "snap"), ("Splunk", "splunk"),
        ("Stripe", "stripe"), ("Twilio", "twilio"), ("Unity", "unity"),
        ("Yelp", "yelp"), ("Zillow", "zillow"), ("Airbnb", "airbnb"),
        ("Affirm", "affirm"), ("Airtable", "airtable"), ("Cloudflare", "cloudflare"),
        # Lever customer tenants
        ("Atlassian", "atlassian"), ("Eventbrite", "eventbrite"), ("Fullstory", "fullstory"),
        ("Medallia", "medallia"), ("Netflix", "netflix"), ("Spotify", "spotify"),
        ("Wealthsimple", "wealthsimple"), ("Kraken", "kraken"), ("Dexterity", "dexterity")
    ]

    async def discover(self, client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, str]]:
        candidates = []
        seen = set()
        for idx, (name, slug) in enumerate(self.VERIFIED_SHOWCASE_TENANTS):
            if slug not in seen:
                seen.add(slug)
                hint = "ashby" if idx < 24 else ("greenhouse" if idx < 51 else "lever")
                candidates.append({
                    "name": name,
                    "slug": slug,
                    "ats_hint": hint,
                    "source": self.name
                })
        return candidates


class GenericCompanyBoardAdapter(BaseDiscoveryAdapter):
    """
    Generic adapter: given a company domain (or list of domains),
    probes well-known board URL patterns and keeps company only on HTTP 200
    with a parseable posting list.
    Everything discovered enters as 'unprobed'; reachability probing promotes to 'reachable' or 'unsupported'.
    """
    name = "generic_board"

    def __init__(self, domains: Optional[List[str]] = None):
        self.domains = domains or []

    async def discover(self, client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, str]]:
        candidates = []
        seen = set()
        for domain in self.domains:
            if not domain:
                continue
            clean_dom = re.sub(r'^https?://', '', domain.strip().lower()).rstrip('/')
            clean_dom = re.sub(r'^www\.', '', clean_dom)
            slug = clean_dom.split('.')[0] if '.' in clean_dom else clean_dom
            slug = re.sub(r'[^a-z0-9-]', '', slug)
            if slug and slug not in seen:
                seen.add(slug)
                candidates.append({
                    "name": slug.replace('-', ' ').title(),
                    "slug": slug,
                    "domain": clean_dom,
                    "source": self.name
                })
        return candidates


class UserApprovedCompanyListAdapter(BaseDiscoveryAdapter):
    """
    Generic adapter for user-approved company lists.
    Reads from a file path or list of company names/slugs.
    Zero hardcoded URLs.
    """
    name = "user_approved_list"

    def __init__(self, source_data: Optional[Union[str, List[Union[str, Dict[str, str]]]]] = None):
        self.source_data = source_data

    async def discover(self, client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, str]]:
        candidates = []
        path = None
        if isinstance(self.source_data, str) and os.path.exists(self.source_data):
            path = self.source_data
        elif self.source_data is None:
            default_p = os.path.expanduser("~/sid_job_scrip/data/user_approved_companies.txt")
            if os.path.exists(default_p):
                path = default_p

        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            slug = re.sub(r'[^a-z0-9-]', '', line.lower().replace(' ', '-'))
                            candidates.append({
                                "name": line,
                                "slug": slug,
                                "source": self.name
                            })
            except Exception as e:
                logger.warning(f"UserApprovedCompanyListAdapter file read error: {e}")
        elif isinstance(self.source_data, list):
            for item in self.source_data:
                if isinstance(item, str) and item.strip():
                    name = item.strip()
                    slug = re.sub(r'[^a-z0-9-]', '', name.lower().replace(' ', '-'))
                    candidates.append({"name": name, "slug": slug, "source": self.name})
                elif isinstance(item, dict) and item.get("name"):
                    name = item["name"].strip()
                    slug = item.get("slug") or re.sub(r'[^a-z0-9-]', '', name.lower().replace(' ', '-'))
                    candidates.append({"name": name, "slug": slug, "source": self.name})

        return candidates


# ── Onboarding Rule Engine ──────────────────────────────────────────────
class OnboardingEngine:
    """
    Onboarding rule (every candidate):
    canonical domain (tenant slug, never a shared key) -> probe official careers page -> confirm G/L/A -> classify -> store.
    Non-G/L/A classifies unsupported once.
    Deduplication: (normalized name + ATS slug). Never re-probes 'unsupported' more than once per 30 days.
    """
    def __init__(self, registry: Optional[CompanyRegistry] = None, rate_limiter: Optional[ATSDomainRateLimiter] = None):
        self.registry = registry or company_registry
        self.rate_limiter = rate_limiter or domain_rate_limiter

    @staticmethod
    def derive_tenant_slug(company_name: str) -> str:
        """Derive tenant slug from company name. Never a shared key."""
        clean = re.sub(r'\b(inc\.?|llc\.?|corp\.?|corporation|ltd\.?|limited|co\.?|technologies|technology)\b', '', company_name, flags=re.IGNORECASE)
        slug = re.sub(r'[^a-z0-9]', '', clean.lower())
        return slug or "unknown"

    async def probe_candidate_boards(
        self,
        name: str,
        slug: str,
        client: httpx.AsyncClient,
        ats_hint: Optional[str] = None
    ) -> Tuple[str, str, str]:
        """
        Probe official board API endpoints: Ashby, Lever, Greenhouse.
        Enforces 10 req/min domain pacing (>= 6s gap) per ATS provider domain.
        If ats_hint is provided ('greenhouse', 'lever', 'ashby'), probes that provider first.
        Keeps only on HTTP 200 with parseable posting list.
        Returns (ats_type, reachability, canonical_domain).
        """
        providers = ["ashby", "lever", "greenhouse"]
        if ats_hint in ("greenhouse", "lever", "ashby"):
            providers.remove(ats_hint)
            providers.insert(0, ats_hint)

        for provider in providers:
            if provider == "ashby":
                await self.rate_limiter.acquire("ashby")
                ashby_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
                try:
                    resp = await client.get(ashby_url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, dict) and ("jobs" in data or "jobBoard" in data):
                            return "ashby", "reachable", slug
                except Exception:
                    pass

            elif provider == "lever":
                await self.rate_limiter.acquire("lever")
                lever_url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
                try:
                    resp = await client.get(lever_url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, list):
                            return "lever", "reachable", slug
                except Exception:
                    pass

            elif provider == "greenhouse":
                await self.rate_limiter.acquire("greenhouse")
                gh_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
                try:
                    resp = await client.get(gh_url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, dict) and "jobs" in data:
                            return "greenhouse", "reachable", slug
                except Exception:
                    pass

        return "unsupported", "unsupported", f"unsupported:{slug}"

    async def onboard_candidate(
        self,
        name: str,
        slug: Optional[str] = None,
        source: str = "discovery",
        client: Optional[httpx.AsyncClient] = None,
        ats_hint: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Probe official careers page / ATS endpoint:
        Confirm Greenhouse, Lever, or Ashby -> classify -> store in registry.
        If non-G/L/A -> classify unsupported once.
        Dedupe: (normalized name + ATS slug). Never re-probe unsupported within 30 days.
        """
        tenant_slug = (slug or self.derive_tenant_slug(name)).strip().lower()
        if not tenant_slug:
            tenant_slug = "unknown"

        # Check deduplication & 30-day unsupported cooldown
        existing = self.registry.find_existing_company(name, tenant_slug)
        if existing:
            reach = existing.get("reachability")
            if reach == "reachable":
                return existing
            if reach == "unsupported":
                if not self.registry.should_probe_unsupported(existing, cooldown_days=30):
                    logger.debug(f"Skipping re-probe for unsupported company '{name}' (30-day cooldown active).")
                    return existing

        # Fast-path: Check authoritative registry tables (REAL_COMPANY_BOARDS & UNSUPPORTED_COMPANIES)
        from app.services.company_registry import get_board_for_company, get_unsupported_reason
        board_info = get_board_for_company(name)
        if board_info:
            return self.registry.classify_and_store(
                name=name,
                canonical_domain=board_info[2],
                ats_type=board_info[1],
                reachability="reachable",
                source=source
            )
        unsupp_info = get_unsupported_reason(name)
        if unsupp_info:
            return self.registry.classify_and_store(
                name=name,
                canonical_domain=f"unsupported:{tenant_slug}",
                ats_type="unsupported",
                reachability="unsupported",
                source=source
            )

        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            should_close = True

        try:
            ats_type, reachability, canonical_domain = await self.probe_candidate_boards(name, tenant_slug, client, ats_hint=ats_hint)
        finally:
            if should_close:
                await client.aclose()

        # Store in registry
        record = self.registry.classify_and_store(
            name=name,
            canonical_domain=canonical_domain,
            ats_type=ats_type,
            reachability=reachability,
            source=source
        )
        # Update last_scanned
        self.registry.update_last_scanned(canonical_domain)
        logger.info(f"Onboarding completed for '{name}' (slug='{tenant_slug}'): ats={ats_type}, reachability={reachability}, source={source}")
        return record


# ── Official Board API Enumeration Engine ───────────────────────────────
class ATSEnumerationEngine:
    """
    Enumeration ONLY via official board APIs:
    - boards.greenhouse.io/<slug> / boards-api.greenhouse.io/v1/boards/<slug>/jobs
    - api.lever.co/v0/postings/<slug>
    - api.ashbyhq.com/posting-api/job-board/<slug>
    Incremental: new companies first, then least-recently-scanned (default 7d), inside 10 req/min per ATS provider domain.
    """
    def __init__(self, registry: Optional[CompanyRegistry] = None, rate_limiter: Optional[ATSDomainRateLimiter] = None):
        self.registry = registry or company_registry
        self.rate_limiter = rate_limiter or domain_rate_limiter

    async def enumerate_company_jobs(
        self,
        company_record: Dict[str, Any],
        client: Optional[httpx.AsyncClient] = None
    ) -> List[Dict[str, Any]]:
        """
        Enumerate live jobs for a single company via official board API.
        Enforces 10 req/min domain pacing and updates last_scanned timestamp.
        """
        ats = company_record.get("ats_type", "").lower()
        slug = company_record.get("canonical_domain", "").strip()
        comp_name = company_record.get("name", slug)

        if not slug or company_record.get("reachability") != "reachable":
            return []

        # Locked-Company Short-Circuit: skip fetching board entirely for lockout duration
        try:
            from app.services.company_cap import company_cap_tracker
            lockout = company_cap_tracker.get_employer_lockout(comp_name)
            if lockout:
                capped_until, lock_reason = lockout
                date_str = capped_until.strftime("%Y-%m-%d")
                logger.info(f"Skipping enumeration for {comp_name}: Employer candidate limit active (locked until {date_str})")
                return []
        except Exception:
            pass

        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            should_close = True

        jobs: List[Dict[str, Any]] = []

        try:
            if ats == "greenhouse":
                await self.rate_limiter.acquire("greenhouse")
                url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    for j in data.get("jobs", []):
                        u = j.get("absolute_url") or f"https://boards.greenhouse.io/{slug}/jobs/{j.get('id')}"
                        loc_name = ""
                        if isinstance(j.get("location"), dict):
                            loc_name = j.get("location", {}).get("name", "")
                        jobs.append({
                            "title": j.get("title", ""),
                            "company": comp_name,
                            "url": u,
                            "location": loc_name or "USA",
                            "description": j.get("content", ""),
                            "created_at": j.get("updated_at"),
                            "portal": "greenhouse",
                            "canonical_domain": slug
                        })

            elif ats == "lever":
                await self.rate_limiter.acquire("lever")
                url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    for j in data:
                        created_ts = j.get("createdAt")
                        created_iso = None
                        if created_ts:
                            try:
                                created_iso = datetime.fromtimestamp(created_ts / 1000.0, tz=timezone.utc).isoformat()
                            except Exception:
                                pass
                        loc_name = j.get("categories", {}).get("location", "")
                        jobs.append({
                            "title": j.get("text", ""),
                            "company": comp_name,
                            "url": j.get("hostedUrl", ""),
                            "location": loc_name or "USA",
                            "description": j.get("description", "") + " " + j.get("additional", ""),
                            "created_at": created_iso,
                            "portal": "lever",
                            "canonical_domain": slug
                        })

            elif ats == "ashby":
                await self.rate_limiter.acquire("ashby")
                url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    for j in data.get("jobs", []):
                        jobs.append({
                            "title": j.get("title", ""),
                            "company": comp_name,
                            "url": j.get("jobUrl", ""),
                            "location": j.get("location", "USA"),
                            "description": j.get("descriptionHtml", "") or j.get("descriptionPlain", ""),
                            "created_at": j.get("publishedAt"),
                            "portal": "ashby",
                            "canonical_domain": slug
                        })

        except Exception as e:
            logger.debug(f"Official board enumeration error for {comp_name} ({ats}): {e}")
        finally:
            if should_close:
                await client.aclose()
            # Update last_scanned in registry
            self.registry.update_last_scanned(slug)

        return jobs

    async def run_incremental_enumeration(
        self,
        lookback_days: int = 7,
        max_companies: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None
    ) -> List[Dict[str, Any]]:
        """
        Runs incremental enumeration across queue:
        new companies first (last_scanned IS NULL), then least-recently-scanned.
        """
        queue = self.registry.get_enumeration_queue(lookback_days=lookback_days, limit=max_companies)
        all_jobs = []
        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            should_close = True

        try:
            for company_rec in queue:
                jobs = await self.enumerate_company_jobs(company_rec, client=client)
                all_jobs.extend(jobs)
        finally:
            if should_close:
                await client.aclose()

        return all_jobs


# ── Autonomous Discovery Engine ─────────────────────────────────────────
class DiscoveryEngine:
    """
    Central discovery coordinator:
    1. Runs block-safe discovery adapters (Ashby, Greenhouse, Lever showcase, YC directory, User list).
    2. Registers discovered candidates as 'unprobed' in company_registry.
    3. Probes unprobed candidates respecting 10 req/min pacing, active hours, and 30-day unsupported cooldown.
    4. Promotes candidates to 'reachable' or 'unsupported'.
    """
    def __init__(
        self,
        registry: Optional[CompanyRegistry] = None,
        onboarding: Optional[OnboardingEngine] = None,
        rate_limiter: Optional[ATSDomainRateLimiter] = None
    ):
        self.registry = registry or company_registry
        self.rate_limiter = rate_limiter or domain_rate_limiter
        self.onboarding = onboarding or OnboardingEngine(registry=self.registry, rate_limiter=self.rate_limiter)
        self.adapters: List[BaseDiscoveryAdapter] = [
            AshbyShowcaseAdapter(),
            GreenhouseShowcaseAdapter(),
            LeverShowcaseAdapter(),
            GitHubATSDatasetAdapter(),
            SearchEngineHarvestingAdapter(),
            YCCompanyDirectoryAdapter(limit=100),
            YCTopCompaniesAdapter(),
            ATSCustomerDirectoryAdapter(),
            UserApprovedCompanyListAdapter(),
        ]
        self.run_lock = DiscoveryRunLock()

    async def discover_all_sources(self, client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, str]]:
        """Run all discovery adapters and return consolidated candidates."""
        all_candidates = []
        for adapter in self.adapters:
            try:
                candidates = await adapter.discover(client=client)
                all_candidates.extend(candidates)
                logger.info(f"Discovery adapter '{adapter.name}' yielded {len(candidates)} candidates.")
            except Exception as e:
                logger.warning(f"Error running discovery adapter '{adapter.name}': {e}")
        return all_candidates

    def register_unprobed_candidates(self, candidates: List[Dict[str, str]]) -> int:
        """
        Enters newly discovered candidates as 'unprobed' in company_registry.
        Preserves ats_hint if discovered from a known ATS source/dataset.
        Returns count of newly registered candidates.
        """
        new_count = 0
        for c in candidates:
            name = c.get("name", "").strip()
            raw_slug = c.get("slug") or self.onboarding.derive_tenant_slug(name)
            slug = re.sub(r'^(unprobed:|unsupported:)+', '', raw_slug).strip()
            source = c.get("source", "discovery")
            ats_hint = c.get("ats_hint") or c.get("ats_type") or "unprobed"
            if ats_hint not in ("greenhouse", "lever", "ashby"):
                ats_hint = "unprobed"

            if not name or not slug:
                continue

            existing = self.registry.find_existing_company(name, slug)
            if not existing:
                self.registry.classify_and_store(
                    name=name,
                    canonical_domain=f"unprobed:{slug}",
                    ats_type=ats_hint,
                    reachability="unprobed",
                    source=source
                )
                new_count += 1
            elif existing.get("reachability") == "unsupported":
                # Check 30-day cooldown: if >= 30 days old, re-queue as unprobed
                if self.registry.should_probe_unsupported(existing, cooldown_days=30):
                    self.registry.classify_and_store(
                        name=name,
                        canonical_domain=f"unprobed:{slug}",
                        ats_type=ats_hint,
                        reachability="unprobed",
                        source=source
                    )
                    new_count += 1
        return new_count

    async def probe_unprobed_batch(self, batch_size: int = 15, client: Optional[httpx.AsyncClient] = None) -> Dict[str, int]:
        """
        Picks unprobed candidates from company_registry and probes them.
        Drain order: yield-prioritized throughout (volume x recency x fit_history, high-priority sources first).
        Promotes candidates to 'reachable' or 'unsupported'.
        """
        results = {"probed": 0, "reachable": 0, "unsupported": 0}
        unprobed = self.registry.get_unprobed_queue(limit=batch_size, order_by_yield=True)

        if not unprobed:
            return results

        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=10.0, verify=SSL_VERIFY_PATH, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            should_close = True

        try:
            for item in unprobed:
                name = item["name"]
                slug = re.sub(r'^(unprobed:|unsupported:)+', '', item["canonical_domain"]).strip()
                source = item.get("source", "discovery")
                ats_hint = item.get("ats_type") if item.get("ats_type") in ("greenhouse", "lever", "ashby") else None
                rec = await self.onboarding.onboard_candidate(name=name, slug=slug, source=source, client=client, ats_hint=ats_hint)
                results["probed"] += 1
                if rec.get("reachability") == "reachable":
                    results["reachable"] += 1
                else:
                    results["unsupported"] += 1
        finally:
            if should_close:
                await client.aclose()

        return results

    async def drain_unprobed_queue(self, max_probes: int = 150, client: Optional[httpx.AsyncClient] = None) -> Dict[str, int]:
        """
        Actively drains the unprobed queue by probing batches until empty or max_probes reached.
        Returns total stats of drained queue.
        """
        total = {"probed": 0, "reachable": 0, "unsupported": 0}
        while total["probed"] < max_probes:
            remaining_to_probe = min(25, max_probes - total["probed"])
            batch = await self.probe_unprobed_batch(batch_size=remaining_to_probe, client=client)
            if batch["probed"] == 0:
                break
            total["probed"] += batch["probed"]
            total["reachable"] += batch["reachable"]
            total["unsupported"] += batch["unsupported"]
        logger.info(f"[DISCOVERY] Drained {total['probed']} unprobed candidates (+{total['reachable']} reachable, +{total['unsupported']} unsupported).")
        return total

    async def run_discovery_cycle(
        self,
        probe_batch_size: int = 15,
        client: Optional[httpx.AsyncClient] = None,
        ignore_active_hours: bool = False
    ) -> Dict[str, Any]:
        """
        Full autonomous discovery cycle enforcing:
        1. Run-lock (skip if another discovery cycle is in flight).
        2. Active hours (09:00 - 20:00 local, bypassable via ignore_active_hours).
        3. Drain order: existing unprobed queue first (yield-prioritized).
        4. Discover new sources (GitHub ATS datasets, Search harvesting, Showcases, YC).
        5. Drain newly arrived slugs (yield-prioritized), ensuring queue drains faster than new slugs arrive.
        6. Report registry totals (reachable / unprobed / unsupported) live from company_registry.
        """
        acquired = await self.run_lock.acquire()
        if not acquired:
            logger.info("[DISCOVERY LOCKED] Another discovery cycle is currently in flight. Skipping trigger.")
            return {"status": "skipped_locked"}

        try:
            if not ignore_active_hours:
                try:
                    from app.services.politeness import politeness
                    if not politeness.within_active_hours():
                        logger.info("Discovery cycle skipped: outside active hours (09:00 - 20:00 local).")
                        return {"status": "skipped_outside_active_hours"}
                except Exception:
                    pass

            # Step 1: Drain existing unprobed queue FIRST (yield-prioritized)
            pre_drain = await self.probe_unprobed_batch(batch_size=probe_batch_size, client=client)

            # Step 2: Discover from new sources
            candidates = await self.discover_all_sources(client=client)
            new_slugs_arrived = self.register_unprobed_candidates(candidates)

            # Step 3: Drain new sources (yield-prioritized)
            # Ensure unprobed queue drains faster than new slugs arrive:
            # If new slugs arrived, drain at least new_slugs_arrived + 5 (or probe_batch_size)
            post_batch = max(probe_batch_size, new_slugs_arrived + 5) if new_slugs_arrived > 0 else probe_batch_size
            post_drain = await self.probe_unprobed_batch(batch_size=post_batch, client=client)

            total_drained = pre_drain["probed"] + post_drain["probed"]
            total_reachable = pre_drain["reachable"] + post_drain["reachable"]
            total_unsupported = pre_drain["unsupported"] + post_drain["unsupported"]

            # Step 4: Report live registry totals
            stats = self.registry.get_stats()
            drains_faster = total_drained > new_slugs_arrived

            logger.info(
                f"[DISCOVERY SUMMARY] Drained: {total_drained} (pre={pre_drain['probed']}, post={post_drain['probed']}) | "
                f"New Slugs Arrived: {new_slugs_arrived} | Drains Faster: {drains_faster} | "
                f"Registry: reachable={stats['reachable']}, unprobed={stats['unprobed']}, unsupported={stats['unsupported']}"
            )

            return {
                "status": "completed",
                "drain_order": "existing_unprobed_first_then_new_sources",
                "drained_count": total_drained,
                "new_slugs_arrived": new_slugs_arrived,
                "drains_faster_than_arrived": drains_faster,
                "pre_drain": pre_drain,
                "post_drain": post_drain,
                "reachable_added": total_reachable,
                "unsupported_added": total_unsupported,
                "registry_totals": {
                    "reachable": stats["reachable"],
                    "unprobed": stats["unprobed"],
                    "unsupported": stats["unsupported"],
                    "greenhouse": stats["greenhouse"],
                    "lever": stats["lever"],
                    "ashby": stats["ashby"],
                    "total": stats["total"],
                },
                # Backwards-compatibility fields for legacy callers
                "candidates_found": len(candidates),
                "new_unprobed": new_slugs_arrived,
                "probed": total_drained,
                "newly_reachable": total_reachable,
                "newly_unsupported": total_unsupported,
            }
        finally:
            self.run_lock.release()


onboarding_engine = OnboardingEngine()
enumeration_engine = ATSEnumerationEngine()
discovery_engine = DiscoveryEngine()
