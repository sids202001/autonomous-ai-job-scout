"""
Unit tests for PolitenessManager rate-limiting, pacing, and active hours.
"""
import unittest
import time
from unittest.mock import patch
from datetime import datetime
from app.services.politeness import PolitenessManager


class TestPolitenessManager(unittest.TestCase):

    def setUp(self):
        self.pm = PolitenessManager()

    def test_daily_application_cap(self):
        with patch("config.settings.MAX_APPLICATIONS_PER_DAY", 3):
            self.assertTrue(self.pm.can_apply_today())
            self.pm.record_application()
            self.pm.record_application()
            self.assertTrue(self.pm.can_apply_today())
            self.pm.record_application()
            self.assertEqual(self.pm.daily_count, 3)
            self.assertFalse(self.pm.can_apply_today())

    def test_within_active_hours(self):
        with patch("config.settings.ACTIVE_HOURS_START", "09:00"), \
             patch("config.settings.ACTIVE_HOURS_END", "20:00"):
            
            # Mock mid-day 14:30
            with patch("app.services.politeness.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 9, 10, 14, 30)
                self.assertTrue(self.pm.within_active_hours())

            # Mock late-night 03:00
            with patch("app.services.politeness.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 9, 10, 3, 0)
                self.assertFalse(self.pm.within_active_hours())

    def test_exponential_backoff_on_429(self):
        domain = "boards-api.greenhouse.io"
        self.pm.record_error(domain, 429)
        state = self.pm._backoff_state[domain]
        self.assertEqual(state["failures"], 1)
        self.assertGreater(state["next_retry"], time.time())

        # Success clears backoff
        self.pm.record_success(domain)
        self.assertEqual(self.pm._backoff_state[domain]["failures"], 0)


if __name__ == "__main__":
    unittest.main()
