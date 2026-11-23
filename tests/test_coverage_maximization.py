"""
Tests for COVERAGE MAXIMIZATION:
1. Slug sources: GitHubATSDatasetAdapter, SearchEngineHarvestingAdapter, Showcase Adapters, YC Directory.
2. Probe and classify: every candidate slug probed via official board API before fetching/applying.
3. Drain order: existing unprobed queue first, then new sources, yield-prioritized throughout.
4. Acceptance: unprobed queue drains faster than new slugs arrive (total_drained > new_slugs_arrived).
5. Registry totals reporting: (reachable / unprobed / unsupported).
6. Standing: official boards only for fetching and applying; slug sources are discovery inputs only.
"""
import os
import sys
import unittest
import asyncio
import tempfile
import sqlite3
from unittest.mock import MagicMock, AsyncMock, patch

if "/Users/siddheshsawant/sid_job_scrip" not in sys.path:
    sys.path.insert(0, "/Users/siddheshsawant/sid_job_scrip")

from app.services.company_registry import CompanyRegistry
from app.services.discovery_engine import (
    ATSDomainRateLimiter,
    OnboardingEngine,
    DiscoveryEngine,
    GitHubATSDatasetAdapter,
    SearchEngineHarvestingAdapter,
    GreenhouseShowcaseAdapter,
    LeverShowcaseAdapter,
    AshbyShowcaseAdapter,
    YCCompanyDirectoryAdapter,
    ATSCustomerDirectoryAdapter,
    ATSEnumerationEngine,
)


class TestCoverageMaximization(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_coverage.db")
        self.registry = CompanyRegistry(db_path=self.db_path)
        self.limiter = ATSDomainRateLimiter(min_gap_seconds=0.0)
        self.onboarding = OnboardingEngine(registry=self.registry, rate_limiter=self.limiter)
        self.discovery = DiscoveryEngine(registry=self.registry, onboarding=self.onboarding, rate_limiter=self.limiter)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_github_ats_dataset_adapter(self):
        """Verifies GitHubATSDatasetAdapter extracts Greenhouse, Lever, and Ashby slugs with ats_hint."""
        adapter = GitHubATSDatasetAdapter()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = """
        | Company | Role | Link |
        | Stripe | SWE | https://boards.greenhouse.io/stripe/jobs/123 |
        | Spotify | Data Engineer | https://jobs.lever.co/spotify/abc-123 |
        | Ramp | Backend | https://jobs.ashbyhq.com/ramp/456 |
        """
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        candidates = asyncio.run(adapter.discover(client=mock_client))
        self.assertGreater(len(candidates), 0)

        slugs = {c["slug"]: c for c in candidates}
        self.assertIn("stripe", slugs)
        self.assertEqual(slugs["stripe"]["ats_hint"], "greenhouse")
        self.assertIn("spotify", slugs)
        self.assertEqual(slugs["spotify"]["ats_hint"], "lever")
        self.assertIn("ramp", slugs)
        self.assertEqual(slugs["ramp"]["ats_hint"], "ashby")

    def test_search_engine_harvesting_adapter(self):
        """Verifies SearchEngineHarvestingAdapter extracts ATS slugs from site: search results."""
        adapter = SearchEngineHarvestingAdapter()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = """
        <a href="https://boards.greenhouse.io/anthropic/jobs/1">Anthropic Careers</a>
        <a href="https://jobs.lever.co/clear/2">Clear Jobs</a>
        <a href="https://jobs.ashbyhq.com/clerk/3">Clerk Careers</a>
        """
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        candidates = asyncio.run(adapter.discover(client=mock_client))
        self.assertGreater(len(candidates), 0)

        slugs = {c["slug"]: c for c in candidates}
        self.assertIn("anthropic", slugs)
        self.assertEqual(slugs["anthropic"]["ats_hint"], "greenhouse")
        self.assertIn("clear", slugs)
        self.assertEqual(slugs["clear"]["ats_hint"], "lever")
        self.assertIn("clerk", slugs)
        self.assertEqual(slugs["clerk"]["ats_hint"], "ashby")

    def test_ats_hint_fast_path_probing(self):
        """Verifies OnboardingEngine prioritizes hinted ATS and avoids probing other providers on 200."""
        mock_client = MagicMock()

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            if "greenhouse.io" in url and "acmecorphinted" in url:
                resp.status_code = 200
                resp.json.return_value = {"jobs": [{"id": 1, "title": "SWE"}]}
            else:
                resp.status_code = 404
                resp.json.return_value = {}
            return resp

        mock_client.get = AsyncMock(side_effect=mock_get)

        # Probing with ats_hint="greenhouse"
        rec = asyncio.run(self.onboarding.onboard_candidate(
            name="AcmeCorpHinted",
            slug="acmecorphinted",
            source="github_ats_datasets",
            client=mock_client,
            ats_hint="greenhouse"
        ))
        self.assertEqual(rec["reachability"], "reachable")
        self.assertEqual(rec["ats_type"], "greenhouse")
        # Ensure only Greenhouse endpoint was queried (Ashby and Lever skipped on success)
        self.assertEqual(mock_client.get.call_count, 1)
        self.assertIn("greenhouse.io", mock_client.get.call_args_list[0][0][0])

    def test_unprobed_queue_yield_prioritization(self):
        """Verifies get_unprobed_queue ranks by expected yield + source priority."""
        # Insert test candidates with different sources
        self.registry.classify_and_store(name="YCDirCo", canonical_domain="unprobed:ycdirco", ats_type="unprobed", reachability="unprobed", source="yc_directory")
        self.registry.classify_and_store(name="YCTopCo", canonical_domain="unprobed:yctopco", ats_type="unprobed", reachability="unprobed", source="yc_top_companies")
        self.registry.classify_and_store(name="GitHubCo", canonical_domain="unprobed:githubco", ats_type="unprobed", reachability="unprobed", source="github_ats_datasets")
        self.registry.classify_and_store(name="SearchCo", canonical_domain="unprobed:searchco", ats_type="unprobed", reachability="unprobed", source="search_engine_harvesting")

        queue = self.registry.get_unprobed_queue(order_by_yield=True)
        names = [q["name"] for q in queue]

        # yc_top_companies (priority 0) before github (priority 2), search (priority 3), and yc_directory (priority 4)
        top_idx = names.index("YCTopCo")
        gh_idx = names.index("GitHubCo")
        search_idx = names.index("SearchCo")
        dir_idx = names.index("YCDirCo")

        self.assertLess(top_idx, gh_idx)
        self.assertLess(gh_idx, search_idx)
        self.assertLess(search_idx, dir_idx)

    def test_drain_order_existing_unprobed_first_then_new_sources(self):
        """Verifies run_discovery_cycle drains existing unprobed queue FIRST, then discovers new sources."""
        # Seed 3 existing unprobed candidates
        self.registry.classify_and_store(name="Existing1", canonical_domain="unprobed:exist1", ats_type="ashby", reachability="unprobed", source="seed")
        self.registry.classify_and_store(name="Existing2", canonical_domain="unprobed:exist2", ats_type="ashby", reachability="unprobed", source="seed")

        probe_order = []

        mock_client = MagicMock()
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            slug = url.split("/")[-1].split("?")[0]
            probe_order.append(slug)
            resp.status_code = 200
            resp.json.return_value = {"jobs": [{"id": 1, "title": "Software Engineer"}]}
            return resp

        mock_client.get = AsyncMock(side_effect=mock_get)

        # Mock adapter returning 1 new company
        mock_adapter = MagicMock()
        mock_adapter.name = "mock_source"
        mock_adapter.discover = AsyncMock(return_value=[
            {"name": "NewArrivalCo", "slug": "newarrivalco", "ats_hint": "ashby", "source": "mock_source"}
        ])
        self.discovery.adapters = [mock_adapter]

        # Run discovery cycle with probe_batch_size=2 and ignore_active_hours=True
        cycle_result = asyncio.run(self.discovery.run_discovery_cycle(probe_batch_size=2, client=mock_client, ignore_active_hours=True))

        self.assertEqual(cycle_result["status"], "completed")
        self.assertEqual(cycle_result["drain_order"], "existing_unprobed_first_then_new_sources")

        # Verify probe_order started with existing unprobed companies
        self.assertIn("exist1", probe_order[:2])
        self.assertIn("exist2", probe_order[:2])
        # And new arrival was probed after existing
        self.assertIn("newarrivalco", probe_order[2:])

    def test_unprobed_queue_drains_faster_than_new_slugs_arrive(self):
        """Verifies acceptance condition: total_drained > new_slugs_arrived."""
        # Pre-seed 5 unprobed
        for i in range(5):
            self.registry.classify_and_store(
                name=f"QueueCo{i}",
                canonical_domain=f"unprobed:queueco{i}",
                ats_type="ashby",
                reachability="unprobed",
                source="seed"
            )

        mock_client = MagicMock()
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"jobs": [{"id": 1, "title": "SWE"}]}
            return resp
        mock_client.get = AsyncMock(side_effect=mock_get)

        # 2 new slugs arrive
        mock_adapter = MagicMock()
        mock_adapter.name = "mock_arrival"
        mock_adapter.discover = AsyncMock(return_value=[
            {"name": "Arrival1", "slug": "arrival1", "ats_hint": "ashby", "source": "mock_arrival"},
            {"name": "Arrival2", "slug": "arrival2", "ats_hint": "ashby", "source": "mock_arrival"},
        ])
        self.discovery.adapters = [mock_adapter]

        cycle_result = asyncio.run(self.discovery.run_discovery_cycle(probe_batch_size=5, client=mock_client, ignore_active_hours=True))

        self.assertEqual(cycle_result["new_slugs_arrived"], 2)
        self.assertGreater(cycle_result["drained_count"], cycle_result["new_slugs_arrived"])
        self.assertTrue(cycle_result["drains_faster_than_arrived"])

        # Check registry totals are reported
        reg_totals = cycle_result["registry_totals"]
        self.assertIn("reachable", reg_totals)
        self.assertIn("unprobed", reg_totals)
        self.assertIn("unsupported", reg_totals)
        self.assertGreater(reg_totals["reachable"], 0)

    def test_official_boards_only_standing_rule(self):
        """Verifies enumeration and application only query official board APIs, never slug sources."""
        enum_engine = ATSEnumerationEngine(registry=self.registry, rate_limiter=self.limiter)

        mock_client = MagicMock()
        queried_urls = []

        async def mock_get(url, **kwargs):
            queried_urls.append(url)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"jobs": [{"id": "1", "title": "SWE", "absolute_url": "https://boards.greenhouse.io/testco/jobs/1"}]}
            return resp

        mock_client.get = AsyncMock(side_effect=mock_get)

        # Company record with source from GitHub dataset
        rec = {
            "name": "TestCo",
            "canonical_domain": "testco",
            "ats_type": "greenhouse",
            "reachability": "reachable",
            "source": "github_ats_datasets"
        }

        jobs = asyncio.run(enum_engine.enumerate_company_jobs(rec, client=mock_client))
        self.assertEqual(len(jobs), 1)
        self.assertIn("https://boards-api.greenhouse.io/v1/boards/testco/jobs?content=true", queried_urls)
        # Verify no github.com or aggregator URLs were ever called
        for u in queried_urls:
            self.assertTrue(
                "boards-api.greenhouse.io" in u or "api.lever.co" in u or "api.ashbyhq.com" in u,
                f"Unauthorized enumeration URL queried: {u}"
            )


if __name__ == "__main__":
    unittest.main()
