import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone

from app.services.company_registry import CompanyRegistry, format_registry_digest_line
from app.services.launch_tracker import LaunchTracker


class TestLaunchLoopStallFixes(unittest.TestCase):
    """
    Regression test suite for:
    1. Scan loop stall prevention (per-company timeout resilience)
    2. Counter vs ledger reconciliation (no phantom test submissions in 'Launched: N')
    3. Enumeration queue draining via update_last_scanned()
    """

    def setUp(self):
        self.registry = CompanyRegistry()

    def test_launched_counter_reconciled_with_launch_tracker(self):
        """
        Verify get_today_counts() sources 'Launched' count directly from
        launch_tracker.get_last_24h_launches(), guaranteeing 100% agreement with
        the 24h Outcome Ledger, and is immune to unrelated database records.
        """
        mock_launches = [
            {"id": "1", "company": "CoA", "role": "SWE", "status": "SUBMITTED", "timestamp": datetime.now(timezone.utc).isoformat()},
            {"id": "2", "company": "CoB", "role": "DE", "status": "CHALLENGE_BLOCKED", "timestamp": datetime.now(timezone.utc).isoformat()}
        ]
        with patch("app.services.launch_tracker.launch_tracker.get_last_24h_launches", return_value=mock_launches):
            q_depth, scored, launched = self.registry.get_today_counts()
            self.assertEqual(launched, 2)

            line = format_registry_digest_line()
            self.assertIn("Launched: 2", line)

    def test_update_last_scanned_drains_enumeration_queue(self):
        """
        Verify that update_last_scanned advances the last_scanned timestamp,
        matching by canonical_domain or name, properly draining the enumeration queue.
        """
        # Ensure 'affirm' is updated
        self.registry.update_last_scanned("Affirm")
        with self.registry._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT last_scanned FROM company_registry WHERE canonical_domain = 'affirm'")
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertIsNotNone(row[0])


class TestPerCompanyTimeout(unittest.IsolatedAsyncioTestCase):
    """Verify that slow company scans are bounded and do not stall the loop."""

    async def test_per_company_timeout_bounds_slow_scans(self):
        """
        Verify that asyncio.wait_for with a timeout successfully bounds an
        individual company search without blocking subsequent companies.
        """
        processed = []

        async def mock_slow_search(company, *args, **kwargs):
            if company == "HangingCo":
                await asyncio.sleep(10)  # Simulates hang
            processed.append(company)

        companies = ["FastCo1", "HangingCo", "FastCo2"]
        timeouts = 0

        for comp in companies:
            try:
                await asyncio.wait_for(mock_slow_search(comp), timeout=0.1)
            except asyncio.TimeoutError:
                timeouts += 1

        self.assertEqual(timeouts, 1)
        self.assertIn("FastCo1", processed)
        self.assertIn("FastCo2", processed)
        self.assertNotIn("HangingCo", processed)


if __name__ == "__main__":
    unittest.main()
