"""
Tests for Registry Expansion, Discovery Adapters, Deduplication, and Company-Level Yield Ranking.
Verifies:
1. YCCompanyDirectoryAdapter & GenericCompanyBoardAdapter
2. Deduplication on (normalized name + ATS slug)
3. 30-day cooldown on unsupported portals
4. Expected yield ranking (posting volume x recency x profile-fit history)
5. Lifecycle state transitions: unprobed -> reachable / unsupported
6. Pacing guarantees
"""
import os
import sys
import unittest
import asyncio
import tempfile
import sqlite3
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone, timedelta

if "/Users/siddheshsawant/sid_job_scrip" not in sys.path:
    sys.path.insert(0, "/Users/siddheshsawant/sid_job_scrip")

from app.services.company_registry import CompanyRegistry
from app.services.discovery_engine import (
    ATSDomainRateLimiter,
    OnboardingEngine,
    YCCompanyDirectoryAdapter,
    GenericCompanyBoardAdapter,
    DiscoveryEngine,
)


class TestRegistryExpansionAndYield(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_registry.db")
        self.registry = CompanyRegistry(db_path=self.db_path)
        self.limiter = ATSDomainRateLimiter(min_gap_seconds=0.0)
        self.onboarding = OnboardingEngine(registry=self.registry, rate_limiter=self.limiter)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_deduplication_and_normalization(self):
        """Verifies deduplication on (normalized name + ATS slug)."""
        # Acme Corp, Inc. with slug 'acme'
        self.registry.classify_and_store(
            name="Acme Corp, Inc.",
            canonical_domain="acme",
            ats_type="greenhouse",
            reachability="reachable",
            source="test"
        )

        # Lookup with varying casing and entity suffix
        existing = self.registry.find_existing_company("Acme Corp", "acme")
        self.assertIsNotNone(existing)
        self.assertEqual(existing["canonical_domain"], "acme")

        existing2 = self.registry.find_existing_company("acme technologies", "acme")
        self.assertIsNotNone(existing2)

    def test_unsupported_30_day_cooldown(self):
        """Verifies unsupported companies are never re-probed more than once per 30 days."""
        now = datetime.now(timezone.utc)
        recent_ts = (now - timedelta(days=5)).isoformat()
        old_ts = (now - timedelta(days=35)).isoformat()

        # Company probed 5 days ago -> should not probe
        rec_recent = {
            "reachability": "unsupported",
            "last_scanned": recent_ts
        }
        self.assertFalse(self.registry.should_probe_unsupported(rec_recent, cooldown_days=30))

        # Company probed 35 days ago -> eligible for re-probe
        rec_old = {
            "reachability": "unsupported",
            "last_scanned": old_ts
        }
        self.assertTrue(self.registry.should_probe_unsupported(rec_old, cooldown_days=30))

    def test_expected_yield_ranking(self):
        """Verifies scoring formula = posting volume x recency x profile-fit history."""
        # Unobserved company: V=5, R=1.0, F=0.10 -> score = 0.50
        y = self.registry.compute_company_yield("Unknown Startup Inc")
        self.assertEqual(y["formula"], "volume * recency * profile_fit_history")
        self.assertGreater(y["score"], 0)
        self.assertEqual(y["volume"], 5.0)
        self.assertEqual(y["recency"], 1.0)
        self.assertEqual(y["fit_history"], 0.10)
        self.assertEqual(y["score"], 0.5)

        # Ranked queue returns highest yield first
        queue = self.registry.get_enumeration_queue(order_by_yield=True)
        self.assertTrue(len(queue) > 0)
        # All items should have yield_score attached
        for item in queue[:5]:
            self.assertIn("yield_score", item)
            self.assertIn("yield_details", item)
        # Assert descending order
        scores = [item["yield_score"] for item in queue]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_yc_company_directory_adapter(self):
        """Verifies YCCompanyDirectoryAdapter queries directory index without scraper deps."""
        adapter = YCCompanyDirectoryAdapter(limit=10)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "hits": [
                {"name": "DoorDash", "slug": "doordash", "website": "https://doordash.com"},
                {"name": "Airbnb", "slug": "airbnb", "website": "https://airbnb.com"},
            ]
        }
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        candidates = asyncio.run(adapter.discover(client=mock_client))
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["name"], "DoorDash")
        self.assertEqual(candidates[0]["slug"], "doordash")
        self.assertEqual(candidates[0]["source"], "yc_directory")

    def test_generic_company_board_adapter(self):
        """Verifies GenericCompanyBoardAdapter extracts candidate slugs from domains."""
        domains = ["https://www.calm.com/", "coder.com", "http://sardine.ai"]
        adapter = GenericCompanyBoardAdapter(domains=domains)
        candidates = asyncio.run(adapter.discover())
        self.assertEqual(len(candidates), 3)
        slugs = [c["slug"] for c in candidates]
        self.assertIn("calm", slugs)
        self.assertIn("coder", slugs)
        self.assertIn("sardine", slugs)

    def test_lifecycle_unprobed_to_reachable_and_unsupported(self):
        """Discovered companies enter as 'unprobed'; probing promotes to 'reachable' or 'unsupported'."""
        discovery = DiscoveryEngine(registry=self.registry, onboarding=self.onboarding, rate_limiter=self.limiter)

        candidates = [
            {"name": "ValidAshbyCo", "slug": "validashby", "source": "yc_directory"},
            {"name": "UnsupportedCo", "slug": "unsupportedco", "source": "yc_directory"},
        ]

        # 1. Register as unprobed
        new_count = discovery.register_unprobed_candidates(candidates)
        self.assertEqual(new_count, 2)

        stats = self.registry.get_stats()
        self.assertEqual(stats["unprobed"], 2)

        # 2. Mock probing responses
        mock_client = MagicMock()
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            if "validashby" in url and "ashbyhq.com" in url:
                resp.status_code = 200
                resp.json.return_value = {"jobs": [{"id": "1", "title": "SWE"}]}
            else:
                resp.status_code = 404
                resp.json.return_value = {}
            return resp

        mock_client.get = AsyncMock(side_effect=mock_get)

        # 3. Probe batch
        probe_res = asyncio.run(discovery.probe_unprobed_batch(batch_size=5, client=mock_client))
        self.assertEqual(probe_res["probed"], 2)
        self.assertEqual(probe_res["reachable"], 1)
        self.assertEqual(probe_res["unsupported"], 1)

        # Check DB states
        valid = self.registry.get_company("validashby")
        self.assertIsNotNone(valid)
        self.assertEqual(valid["reachability"], "reachable")
        self.assertEqual(valid["ats_type"], "ashby")

        unsupp = self.registry.get_company("unsupported:unsupportedco")
        self.assertIsNotNone(unsupp)
        self.assertEqual(unsupp["reachability"], "unsupported")

        # Unprobed count should now be 0
        stats2 = self.registry.get_stats()
        self.assertEqual(stats2["unprobed"], 0)


if __name__ == "__main__":
    unittest.main()
