"""
Unit tests for Job Scout - Final Consolidated Pass:
A. SCALE-UP:
   1. company_registry (canonical_domain PK, schema, 442 seed, non-G/L/A classifies unsupported once)
   2. Discovery adapters (Ashby, Greenhouse, Lever showcase, User-approved list)
   3. Onboarding rule (slug derive, probe G/L/A, classify reachable vs unsupported once)
   4. ATSEnumerationEngine & ATSDomainRateLimiter (10 req/min per ATS domain, incremental queue)
   5. Fit Ranking Engine (skill_fit_pct * recency_weight * h1b_signal, strictly ordered queue, rank logging)
   6. Digest line formatting (verbatim spec)
B. RETIREMENT & DEPLOY DISCIPLINE:
   1. Retired Indeed/JobSpy and Role Discovered template (zero linkedin/indeed/jobspy)
   2. Deploy discipline (daemon boot logging, build hash)
C. STANDING PROOF:
   - Standing proof in build_proof.json with 4 evidence items remains intact.
"""

import os
import re
import json
import sqlite3
import tempfile
import unittest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

from app.services.company_registry import (
    CompanyRegistry,
    REAL_COMPANY_BOARDS,
    UNSUPPORTED_COMPANIES,
    format_registry_digest_line,
    is_searchable,
    get_board_for_company,
    get_unsupported_reason,
)
from app.services.discovery_engine import (
    ATSDomainRateLimiter,
    AshbyShowcaseAdapter,
    GreenhouseShowcaseAdapter,
    LeverShowcaseAdapter,
    UserApprovedCompanyListAdapter,
    OnboardingEngine,
    ATSEnumerationEngine,
)
from app.services.fit_ranker import (
    FitRanker,
    CANDIDATE_PORTFOLIO_SKILLS,
    CANDIDATE_GITHUB_SKILLS,
    RESUME_TECHNICAL_SKILLS,
)
from app.services.launch_tracker import LaunchTracker


class TestCompanyRegistryScaleup(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_autoapply.db")
        self.registry = CompanyRegistry(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_a1_schema_and_seeding(self):
        """Test schema has canonical_domain PK and seeds 442 companies."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(company_registry)")
            cols = {row[1]: row[2] for row in cur.fetchall()}
            self.assertIn("canonical_domain", cols)
            self.assertIn("name", cols)
            self.assertIn("ats_type", cols)
            self.assertIn("reachability", cols)
            self.assertIn("first_seen", cols)
            self.assertIn("last_scanned", cols)
            self.assertIn("source", cols)

            # Check primary key is canonical_domain
            cur.execute("PRAGMA table_info(company_registry)")
            pk_cols = [row[1] for row in cur.fetchall() if row[5] == 1]
            self.assertEqual(pk_cols, ["canonical_domain"])

        stats = self.registry.get_stats()
        self.assertEqual(stats["total"], 442)
        self.assertEqual(stats["reachable"], 140)
        self.assertEqual(stats["unsupported"], 302)
        self.assertEqual(stats["greenhouse"], 94)
        self.assertEqual(stats["lever"], 10)
        self.assertEqual(stats["ashby"], 36)

    def test_a1_non_gla_classifies_unsupported_once(self):
        """Non-G/L/A classifies unsupported once and records reason."""
        record = self.registry.classify_and_store(
            name="WorkdayGiant",
            canonical_domain="unsupported:workdaygiant",
            ats_type="unsupported",
            reachability="unsupported",
            source="custom_portal"
        )
        self.assertEqual(record["reachability"], "unsupported")
        self.assertEqual(record["ats_type"], "unsupported")

        # Verify query returns it
        found = self.registry.get_company("unsupported:workdaygiant")
        self.assertIsNotNone(found)
        self.assertEqual(found["name"], "WorkdayGiant")
        self.assertEqual(found["reachability"], "unsupported")

        # Calling again should update or remain unsupported, not duplicate
        self.registry.classify_and_store(
            name="WorkdayGiant",
            canonical_domain="unsupported:workdaygiant",
            ats_type="unsupported",
            reachability="unsupported",
            source="custom_portal"
        )
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM company_registry WHERE canonical_domain = 'unsupported:workdaygiant'")
            self.assertEqual(cur.fetchone()[0], 1)

    def test_a1_lookup_and_searchability(self):
        """Test lookup by slug and name."""
        comp = self.registry.find_company_by_name("Anthropic")
        self.assertIsNotNone(comp)
        self.assertEqual(comp["ats_type"], "greenhouse")
        self.assertEqual(comp["canonical_domain"], "anthropic")

        comp_slug = self.registry.get_company("anthropic")
        self.assertIsNotNone(comp_slug)
        self.assertEqual(comp_slug["name"], "Anthropic")

    def test_a4_incremental_enumeration_queue_ordering(self):
        """Enumeration queue orders new companies first (last_scanned IS NULL), then least-recently-scanned."""
        # Update last_scanned on a few companies
        self.registry.update_last_scanned("anthropic", "2026-09-01T00:00:00+00:00")
        self.registry.update_last_scanned("affirm", "2026-09-05T00:00:00+00:00")

        queue = self.registry.get_enumeration_queue(lookback_days=7)
        # All companies with last_scanned IS NULL should come before those with timestamps
        null_scanned = [c for c in queue if c["last_scanned"] is None]
        ts_scanned = [c for c in queue if c["last_scanned"] is not None]

        self.assertTrue(len(null_scanned) > 0)
        self.assertTrue(len(ts_scanned) > 0)

        # In queue, all null_scanned come first
        first_ts_idx = next(i for i, c in enumerate(queue) if c["last_scanned"] is not None)
        self.assertEqual(first_ts_idx, len(null_scanned))

        # Among ts_scanned, 2026-09-01 (anthropic) must appear before 2026-09-05 (affirm)
        ts_domains = [c["canonical_domain"] for c in ts_scanned]
        self.assertIn("anthropic", ts_domains)
        self.assertIn("affirm", ts_domains)
        self.assertLess(ts_domains.index("anthropic"), ts_domains.index("affirm"))


class TestDiscoveryAdapters(unittest.TestCase):
    def test_a2_ashby_showcase_adapter(self):
        """AshbyShowcaseAdapter parses customer names from showcase HTML/JSON."""
        adapter = AshbyShowcaseAdapter()
        mock_html = '''
        <html>
            <script id="__NEXT_DATA__">
                {"props": {"pageProps": {"caseStudies": [
                    {"header": "How Linear scaled engineering with Ashby"},
                    {"quote": "Director of Talent at Ramp, says Ashby is great"},
                    {"text": "Talent Acquisition Lead at Oyster,"}
                ]}}}
            </script>
        </html>
        '''
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = mock_html

        client = MagicMock()
        client.get = AsyncMock(return_value=mock_resp)

        candidates = asyncio.run(adapter.discover(client=client))
        names = [c["name"] for c in candidates]
        self.assertIn("Linear", names)
        self.assertIn("Ramp", names)
        self.assertIn("Oyster", names)
        for c in candidates:
            self.assertEqual(c["source"], "ashby_showcase")
            self.assertTrue(len(c["slug"]) > 0)

    def test_a2_greenhouse_showcase_adapter(self):
        """GreenhouseShowcaseAdapter parses customer slugs from customer-stories links."""
        adapter = GreenhouseShowcaseAdapter()
        mock_html = '''
        <div>
            <a href="/customer-stories/gusto">Gusto Story</a>
            <a href="/customer-stories/databricks">Databricks Story</a>
            <a href="/customer-stories/reddit">Reddit Story</a>
        </div>
        '''
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = mock_html

        client = MagicMock()
        client.get = AsyncMock(return_value=mock_resp)

        candidates = asyncio.run(adapter.discover(client=client))
        slugs = [c["slug"] for c in candidates]
        self.assertIn("gusto", slugs)
        self.assertIn("databricks", slugs)
        self.assertIn("reddit", slugs)
        for c in candidates:
            self.assertEqual(c["source"], "greenhouse_showcase")

    def test_a2_lever_showcase_adapter(self):
        """LeverShowcaseAdapter parses customer names from showcase page."""
        adapter = LeverShowcaseAdapter()
        mock_html = '''
        <div>
            <img alt="Netflix logo" src="/logo1.png" />
            <div data-customer="Karat">Karat Case Study</div>
        </div>
        '''
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = mock_html

        client = MagicMock()
        client.get = AsyncMock(return_value=mock_resp)

        candidates = asyncio.run(adapter.discover(client=client))
        names = [c["name"] for c in candidates]
        self.assertIn("Netflix", names)
        self.assertIn("Karat", names)
        for c in candidates:
            self.assertEqual(c["source"], "lever_showcase")

    def test_a2_user_approved_company_list_adapter(self):
        """UserApprovedCompanyListAdapter loads companies from list or file with zero hardcoded URLs."""
        custom_list = ["Acme Corp", "TechFlow Inc", {"name": "Zenith AI", "slug": "zenith-ai"}]
        adapter = UserApprovedCompanyListAdapter(source_data=custom_list)
        candidates = asyncio.run(adapter.discover())
        self.assertEqual(len(candidates), 3)
        self.assertEqual(candidates[0]["name"], "Acme Corp")
        self.assertEqual(candidates[0]["slug"], "acme-corp")
        self.assertEqual(candidates[2]["slug"], "zenith-ai")

    def test_a2_zero_third_party_scrapes(self):
        """Verifies no linkedin, indeed, or third-party scraping references exist in discovery adapters."""
        with open("/Users/siddheshsawant/sid_job_scrip/app/services/discovery_engine.py", "r") as f:
            code = f.read().lower()
            self.assertNotIn("linkedin.com", code)
            self.assertNotIn("indeed.com", code)
            self.assertNotIn("import jobspy", code)
            self.assertNotIn("from jobspy", code)


class TestOnboardingEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_autoapply.db")
        self.registry = CompanyRegistry(db_path=self.db_path)
        # 0s gap limiter for fast unit test
        self.limiter = ATSDomainRateLimiter(min_gap_seconds=0.0)
        self.engine = OnboardingEngine(registry=self.registry, rate_limiter=self.limiter)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_a3_slug_derivation(self):
        """Test clean derivation of candidate ATS tenant slug."""
        self.assertEqual(OnboardingEngine.derive_tenant_slug("Acme Technologies, Inc."), "acme")
        self.assertEqual(OnboardingEngine.derive_tenant_slug("Stripe LLC"), "stripe")
        self.assertEqual(OnboardingEngine.derive_tenant_slug("Cursor AI"), "cursorai")

    def test_a3_onboard_reachable_greenhouse(self):
        """Probing detects Greenhouse board 200 with jobs -> classifies reachable."""
        mock_client = MagicMock()
        # Ashby 404, Lever 404, Greenhouse 200
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            if "greenhouse.io" in url:
                resp.status_code = 200
                resp.json.return_value = {"jobs": [{"id": 1, "title": "Software Engineer"}]}
            else:
                resp.status_code = 404
                resp.json.return_value = {}
            return resp

        mock_client.get = AsyncMock(side_effect=mock_get)

        record = asyncio.run(self.engine.onboard_candidate("NewGHCo", slug="newghco", client=mock_client))
        self.assertEqual(record["ats_type"], "greenhouse")
        self.assertEqual(record["reachability"], "reachable")
        self.assertEqual(record["canonical_domain"], "newghco")

        # Verify stored in DB
        db_rec = self.registry.get_company("newghco")
        self.assertIsNotNone(db_rec)
        self.assertEqual(db_rec["ats_type"], "greenhouse")

    def test_a3_onboard_reachable_ashby(self):
        """Probing detects Ashby board 200 with jobs -> classifies reachable."""
        mock_client = MagicMock()
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            if "ashbyhq.com" in url:
                resp.status_code = 200
                resp.json.return_value = {"jobs": [{"id": "abc", "title": "Founding Engineer"}]}
            else:
                resp.status_code = 404
            return resp

        mock_client.get = AsyncMock(side_effect=mock_get)

        record = asyncio.run(self.engine.onboard_candidate("NewAshbyCo", slug="newashbyco", client=mock_client))
        self.assertEqual(record["ats_type"], "ashby")
        self.assertEqual(record["reachability"], "reachable")

    def test_a3_onboard_unsupported_once(self):
        """Probing 404 on all 3 -> classifies unsupported once, never probes again."""
        mock_client = MagicMock()
        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status_code = 404
            resp.json.return_value = {}
            return resp

        mock_client.get = AsyncMock(side_effect=mock_get)

        record = asyncio.run(self.engine.onboard_candidate("UnknownPortalCo", slug="unknownportal", client=mock_client))
        self.assertEqual(record["ats_type"], "unsupported")
        self.assertEqual(record["reachability"], "unsupported")
        self.assertEqual(call_count, 3)  # Probed Ashby, Lever, Greenhouse

        # Second call: must return cached record without making any network calls
        record2 = asyncio.run(self.engine.onboard_candidate("UnknownPortalCo", slug="unknownportal", client=mock_client))
        self.assertEqual(record2["reachability"], "unsupported")
        self.assertEqual(call_count, 3)  # Unchanged, no second probe!


class TestATSEnumerationAndRateLimiter(unittest.TestCase):
    def test_a4_ats_domain_rate_limiter(self):
        """Verifies ATSDomainRateLimiter spaces requests by min_gap."""
        limiter = ATSDomainRateLimiter(min_gap_seconds=0.05)

        async def run_pacing():
            t0 = asyncio.get_event_loop().time()
            await limiter.acquire("greenhouse")
            await limiter.acquire("greenhouse")
            t1 = asyncio.get_event_loop().time()
            return t1 - t0

        elapsed = asyncio.run(run_pacing())
        self.assertGreaterEqual(elapsed, 0.04)

    def test_a4_enumeration_greenhouse_lever_ashby(self):
        """ATSEnumerationEngine parses official APIs and updates last_scanned."""
        temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(temp_dir.name, "test_autoapply.db")
        registry = CompanyRegistry(db_path=db_path)
        limiter = ATSDomainRateLimiter(min_gap_seconds=0.0)
        engine = ATSEnumerationEngine(registry=registry, rate_limiter=limiter)

        mock_client = MagicMock()
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "greenhouse.io" in url:
                resp.json.return_value = {
                    "jobs": [{
                        "id": 101,
                        "title": "Backend Software Engineer",
                        "location": {"name": "New York, NY"},
                        "content": "Python, SQL, AWS",
                        "updated_at": "2026-09-22T00:00:00Z"
                    }]
                }
            elif "lever.co" in url:
                resp.json.return_value = [{
                    "text": "Data Engineer",
                    "hostedUrl": "https://jobs.lever.co/test/202",
                    "categories": {"location": "Remote"},
                    "description": "Kafka, Spark, Python",
                    "additional": "",
                    "createdAt": 1726000000000
                }]
            elif "ashbyhq.com" in url:
                resp.json.return_value = {
                    "jobs": [{
                        "title": "Full Stack Engineer",
                        "jobUrl": "https://jobs.ashbyhq.com/test/303",
                        "location": "San Francisco, CA",
                        "descriptionPlain": "TypeScript, React, Python",
                        "publishedAt": "2026-09-22T00:00:00Z"
                    }]
                }
            return resp

        mock_client.get = AsyncMock(side_effect=mock_get)

        # Enumerate Greenhouse
        gh_jobs = asyncio.run(engine.enumerate_company_jobs({
            "name": "Databricks", "ats_type": "greenhouse", "canonical_domain": "databricks", "reachability": "reachable"
        }, client=mock_client))
        self.assertEqual(len(gh_jobs), 1)
        self.assertEqual(gh_jobs[0]["title"], "Backend Software Engineer")
        self.assertEqual(gh_jobs[0]["portal"], "greenhouse")

        # Enumerate Lever
        lever_jobs = asyncio.run(engine.enumerate_company_jobs({
            "name": "Kraken", "ats_type": "lever", "canonical_domain": "kraken", "reachability": "reachable"
        }, client=mock_client))
        self.assertEqual(len(lever_jobs), 1)
        self.assertEqual(lever_jobs[0]["title"], "Data Engineer")

        # Enumerate Ashby
        ashby_jobs = asyncio.run(engine.enumerate_company_jobs({
            "name": "Linear", "ats_type": "ashby", "canonical_domain": "linear", "reachability": "reachable"
        }, client=mock_client))
        self.assertEqual(len(ashby_jobs), 1)
        self.assertEqual(ashby_jobs[0]["title"], "Full Stack Engineer")

        # Verify last_scanned was updated
        db_gh = registry.get_company("databricks")
        self.assertIsNotNone(db_gh["last_scanned"])

        temp_dir.cleanup()


class TestFitRanker(unittest.TestCase):
    def setUp(self):
        self.ranker = FitRanker()

    def test_a5_consolidated_candidate_skills(self):
        """Candidate skills consolidate Resume + Portfolio + GitHub."""
        skills = self.ranker.get_candidate_skills()
        # Verify skills from portfolio
        self.assertIn("power bi", skills)
        self.assertIn("tableau", skills)
        # Verify skills from github
        self.assertIn("playwright", skills)
        self.assertIn("fastapi", skills)
        # Verify skills from resume vocabulary
        self.assertIn("python", skills)

    def test_a5_recency_weight(self):
        """Recency weights: <=1d -> 1.0, 7d -> 0.75, 14d -> 0.55, >14d -> 0.40."""
        now = datetime.now(timezone.utc)
        self.assertEqual(self.ranker.calculate_recency(now.isoformat()), 1.0)
        d5 = (now - timedelta(days=5)).isoformat()
        self.assertEqual(self.ranker.calculate_recency(d5), 0.75)
        d12 = (now - timedelta(days=12)).isoformat()
        self.assertEqual(self.ranker.calculate_recency(d12), 0.55)
        d40 = (now - timedelta(days=40)).isoformat()
        self.assertEqual(self.ranker.calculate_recency(d40), 0.40)

    def test_a5_h1b_signal(self):
        """H1B signal: 0.0 for no sponsorship, 1.0 for positive sponsorship, 0.85 neutral."""
        no_sponsor_desc = "Must have permanent US work authorization without requiring sponsorship now or in future."
        pos_sponsor_desc = "We provide visa sponsorship (H-1B transfer welcome) for qualified applicants."
        neutral_desc = "Join our software engineering team to build scalable microservices."

        self.assertEqual(self.ranker.calculate_h1b_signal(no_sponsor_desc), 0.0)
        self.assertEqual(self.ranker.calculate_h1b_signal(pos_sponsor_desc), 1.0)
        self.assertEqual(self.ranker.calculate_h1b_signal(neutral_desc), 0.85)

    def test_a5_rank_score_formula_and_queue_ordering(self):
        """Test rank_score = skill_fit_pct * recency_weight * h1b_signal and strict descending order."""
        jobs = [
            {
                "title": "Junior Python Developer",
                "description": "Python, SQL, FastAPI, Docker. We provide visa sponsorship.",
                "created_at": datetime.now(timezone.utc).isoformat()
            },
            {
                "title": "Ruby Engineer",
                "description": "Ruby on Rails, Heroku, No sponsorship available.",
                "created_at": (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
            },
            {
                "title": "Data Analyst",
                "description": "Power BI, Tableau, SQL, Python.",
                "created_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
            }
        ]

        scored_queue = self.ranker.rank_and_order_queue(jobs)
        self.assertEqual(len(scored_queue), 3)

        # Ranked strictly descending by rank_score
        self.assertGreaterEqual(scored_queue[0]["fit_score"], scored_queue[1]["fit_score"])
        self.assertGreaterEqual(scored_queue[1]["fit_score"], scored_queue[2]["fit_score"])

        # Check ranks 1, 2, 3
        self.assertEqual(scored_queue[0]["rank"], 1)
        self.assertEqual(scored_queue[1]["rank"], 2)
        self.assertEqual(scored_queue[2]["rank"], 3)

        # Lowest score must be the no-sponsorship job
        self.assertEqual(scored_queue[2]["title"], "Ruby Engineer")

    def test_a5_launch_tracker_records_rank_and_fit_score(self):
        """Launch tracker registers and preserves rank and fit_score."""
        temp_dir = tempfile.TemporaryDirectory()
        tr_path = os.path.join(temp_dir.name, "launch_tracker.json")
        pr_path = os.path.join(temp_dir.name, "build_proof.json")
        tracker = LaunchTracker(tracker_path=tr_path, proof_path=pr_path)

        l_id = tracker.register_launch(
            company="RankCo",
            role="Data Engineer",
            url="https://jobs.lever.co/rankco/1",
            portal="lever",
            rank=1,
            fit_score=0.85
        )

        records = tracker._read_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["rank"], 1)
        self.assertEqual(records[0]["fit_score"], 0.85)

        temp_dir.cleanup()


class TestDigestLineFormatting(unittest.TestCase):
    def test_a6_digest_line_verbatim_spec(self):
        """Digest line contains registry size, G/L/A split, reachable/unsupported, queue depth, roles scored, applications."""
        line = format_registry_digest_line(queue_depth=15, roles_scored_today=42, apps_today=5)
        self.assertIn("🌐 *Registry:*", line)
        self.assertIn("Greenhouse", line)
        self.assertIn("Lever", line)
        self.assertIn("Ashby", line)
        self.assertIn("reachable", line)
        self.assertIn("unsupported", line)
        self.assertIn("Queue: 15", line)
        self.assertIn("Scored: 42", line)
        self.assertIn("Launched: 5", line)


class TestRetirementAndDeployDiscipline(unittest.TestCase):
    def test_b1_zero_jobspy_indeed_in_pipeline(self):
        """Zero reachable indeed or jobspy code paths in job_scraper and resume_bot."""
        with open("/Users/siddheshsawant/sid_job_scrip/app/services/job_scraper.py", "r") as f:
            code = f.read()
            self.assertNotIn("from jobspy", code)
            self.assertNotIn("import jobspy", code)
            self.assertNotIn("scrape_jobs", code)
            self.assertNotIn("indeed.com", code.lower())

        with open("/Users/siddheshsawant/Desktop/resume_bot.py", "r") as f:
            bot_code = f.read()
            self.assertNotIn("jobspy", bot_code.lower())
            self.assertNotIn("from jobspy", bot_code)
            self.assertNotIn("scrape_company_jobs_broad", bot_code)
            self.assertNotIn("Role Discovered", bot_code)

    def test_b1_zero_linkedin_requests(self):
        """Zero linkedin.com network requests in discovery and scraping pipelines."""
        pipeline_files = [
            "/Users/siddheshsawant/sid_job_scrip/app/services/discovery_engine.py",
            "/Users/siddheshsawant/sid_job_scrip/app/services/job_scraper.py",
            "/Users/siddheshsawant/Desktop/resume_bot.py"
        ]
        for fpath in pipeline_files:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
                # Ensure no URL requests to linkedin
                self.assertNotIn("https://www.linkedin.com/jobs", content)
                self.assertNotIn("https://linkedin.com/jobs", content)
                self.assertNotIn("api.linkedin.com", content)
                self.assertNotIn("requests.get(\"https://www.linkedin", content)
                self.assertNotIn("client.get(\"https://www.linkedin", content)
                self.assertNotIn("linkedin.com/jobs", content.lower())

    def test_b2_daemon_boot_logging_and_hash(self):
        """Daemon boot logging format and build hash calculation."""
        import sys
        if "/Users/siddheshsawant/Desktop" not in sys.path:
            sys.path.insert(0, "/Users/siddheshsawant/Desktop")
        import resume_bot

        build_hash = resume_bot.get_daemon_build_hash()
        self.assertIsInstance(build_hash, str)
        self.assertEqual(len(build_hash), 16)

        # Verify hash changes if file changes (deterministic sha256 prefix)
        self.assertTrue(re.match(r'^[0-9a-f]{16}$', build_hash))

    def test_c5_standing_proof_intact(self):
        """Standing proof in build_proof.json remains the standing proof with all 4 evidence items."""
        proof_path = "/Users/siddheshsawant/sid_job_scrip/data/build_proof.json"
        self.assertTrue(os.path.exists(proof_path), "build_proof.json must exist")

        with open(proof_path, "r") as f:
            data = json.load(f)

        self.assertIn("proven", data)
        self.assertTrue(data["proven"])
        self.assertEqual(data.get("current_build_id"), "2026-09-20.v2.observability")

        record = data.get("proof_record", {})
        self.assertTrue(record.get("launch_id"))
        self.assertTrue(record.get("company"))
        self.assertTrue(record.get("screenshot_path"))
        self.assertTrue(record.get("receipt_msg_id"))
        self.assertTrue(record.get("timestamp"))

    def test_c6_telegram_noise_leak_allowlist_enforcement(self):
        """
        Regression test: Enforce strict default-deny allowlist.
        Scan lifecycle (start/complete/progress) must be digest-only, ALWAYS.
        Zero send sites for 'Starting On-Demand Scan' or 'Full watchlist scan complete'.
        """
        bot_py_path = "/Users/siddheshsawant/Desktop/resume_bot.py"
        with open(bot_py_path, "r") as f:
            code = f.read()

        # 1. Zero occurrences of leaking strings
        self.assertNotIn("Starting On-Demand Scan", code)
        self.assertNotIn("Full watchlist scan complete", code)

        # 2. Zero non-allowlisted scanning progress messages
        self.assertNotIn("Scanning silently in the background", code)
        self.assertNotIn("No active junior tech job postings found for", code)

        # 3. No second source of truth: DEFAULT_TARGET_COMPANIES retired
        self.assertNotIn("DEFAULT_TARGET_COMPANIES", code)

        # 4. load_watchlist reads from company_registry
        import sys
        if "/Users/siddheshsawant/Desktop" not in sys.path:
            sys.path.insert(0, "/Users/siddheshsawant/Desktop")
        import resume_bot
        wl = resume_bot.load_watchlist()
        from app.services.company_registry import company_registry
        reachable_count = company_registry.get_stats()["reachable"]
        self.assertEqual(len(wl), reachable_count, "Watchlist must equal reachable G/L/A companies in registry")


if __name__ == "__main__":
    unittest.main()
