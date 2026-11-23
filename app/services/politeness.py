"""
AutoApply - Politeness Module
Responsible rate-limiting to avoid hammering job portals.

Why each limit exists:
- max_applications_per_day: Prevents account flagging on ATS platforms
- min/max_delay: Mimics human browsing patterns (5-15 min gaps)
- active_hours: Only applies during business hours to appear natural
- per_domain_rate: Respects individual portal rate limits
- exponential_backoff: Backs off gracefully on 429/5xx responses
"""
import asyncio
import logging
import random
import time
from datetime import datetime
from collections import defaultdict
import os

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
# Create data dir if not exists
os.makedirs("data", exist_ok=True)
fh = logging.FileHandler("data/politeness.log")
fh.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(fh)

# Per-domain request limits (requests per minute)
DOMAIN_LIMITS = {
    'greenhouse.io': 10,
    'boards-api.greenhouse.io': 10,
    'api.lever.co': 10,
    'lever.co': 10,
    'ashbyhq.com': 10,
    'api.ashbyhq.com': 10,
}

class PolitenessManager:
    def __init__(self):
        self._daily_count = 0
        self._daily_reset_date = None
        self._domain_timestamps = defaultdict(list)  # domain -> list of request timestamps
        self._backoff_state = defaultdict(lambda: {'failures': 0, 'next_retry': 0})
        self._portal_locks = defaultdict(asyncio.Lock)

    def get_portal_lock(self, portal_or_url: str) -> asyncio.Lock:
        """
        Enforces Anti-Block Hygiene Guarantee:
        One session per ATS portal at a time — no concurrent applications to the same portal.
        """
        domain = self._extract_portal_domain(portal_or_url)
        return self._portal_locks[domain]

    @staticmethod
    def _extract_portal_domain(portal_or_url: str) -> str:
        s = (portal_or_url or "").lower()
        if "greenhouse" in s:
            return "greenhouse.io"
        elif "lever" in s:
            return "lever.co"
        elif "ashby" in s:
            return "ashbyhq.com"
        elif "workday" in s:
            return "myworkdayjobs.com"
        elif "smartrecruiters" in s:
            return "smartrecruiters.com"
        return "direct"
    
    def _reset_daily_if_needed(self):
        today = datetime.now().date()
        if self._daily_reset_date != today:
            self._daily_count = 0
            self._daily_reset_date = today
            logger.info('Daily application counter reset')
    
    def within_active_hours(self) -> bool:
        from config import settings
        now = datetime.now()
        try:
            start_h, start_m = map(int, settings.ACTIVE_HOURS_START.split(':'))
            end_h, end_m = map(int, settings.ACTIVE_HOURS_END.split(':'))
            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m
            current_minutes = now.hour * 60 + now.minute
            return start_minutes <= current_minutes <= end_minutes
        except Exception:
            return True  # fail open
    
    def can_apply_today(self) -> bool:
        from config import settings
        self._reset_daily_if_needed()
        return self._daily_count < settings.MAX_APPLICATIONS_PER_DAY
    
    def record_application(self):
        self._reset_daily_if_needed()
        self._daily_count += 1
        logger.info(f'Daily application count: {self._daily_count}')
    
    @property
    def daily_count(self) -> int:
        self._reset_daily_if_needed()
        return self._daily_count
    
    async def pace_between_applications(self):
        from config import settings
        delay = random.uniform(settings.MIN_DELAY_BETWEEN_APPS, settings.MAX_DELAY_BETWEEN_APPS)
        logger.info(f'Pacing: waiting {delay:.0f}s before next application')
        await asyncio.sleep(delay)
    
    async def respect_rate_limit(self, domain: str):
        # Clean old timestamps (older than 60s)
        now = time.time()
        self._domain_timestamps[domain] = [
            ts for ts in self._domain_timestamps[domain] if now - ts < 60
        ]
        
        # Find applicable limit
        limit = 10  # default
        for pattern, rpm in DOMAIN_LIMITS.items():
            if pattern in domain:
                limit = rpm
                break
        
        if len(self._domain_timestamps[domain]) >= limit:
            oldest = self._domain_timestamps[domain][0]
            wait_time = max(0.0, 60.0 - (now - oldest))
            if wait_time > 0:
                logger.info(f'Rate limit: waiting {wait_time:.1f}s for {domain}')
                await asyncio.sleep(wait_time)
        
        # Check backoff state
        backoff = self._backoff_state[domain]
        if backoff['next_retry'] > now:
            wait = backoff['next_retry'] - now
            logger.info(f'Backoff: waiting {wait:.1f}s for {domain}')
            await asyncio.sleep(wait)
        
        self._domain_timestamps[domain].append(time.time())
    
    def record_error(self, domain: str, status_code: int):
        if status_code in (429, 500, 502, 503, 504):
            state = self._backoff_state[domain]
            state['failures'] += 1
            # Exponential backoff with jitter: 2^failures * (30-60s)
            base_delay = min(2 ** state['failures'] * random.uniform(30, 60), 1800)
            state['next_retry'] = time.time() + base_delay
            logger.warning(f'Backoff for {domain}: {base_delay:.0f}s after {state["failures"]} failures (HTTP {status_code})')
    
    def record_success(self, domain: str):
        if domain in self._backoff_state:
            self._backoff_state[domain] = {'failures': 0, 'next_retry': 0}

# Singleton
politeness = PolitenessManager()
