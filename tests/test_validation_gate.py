"""
Unit tests for AutoFiller pre-submit validation gate.
"""
import unittest
from app.services.auto_filler import AutoFiller


class TestValidationGate(unittest.TestCase):

    def setUp(self):
        self.filler = AutoFiller()

    def test_missing_core_fields_fails(self):
        result = {
            "fields_filled": {},
            "resume_attached": True,
            "captcha_detected": False,
        }
        valid, reasons = self.filler.validate_before_submit(result)
        self.assertFalse(valid)
        self.assertTrue(any("core fields" in r for r in reasons))

    def test_captcha_blocking_fails(self):
        result = {
            "fields_filled": {"email": True, "first_name": True},
            "resume_attached": True,
            "captcha_detected": True,
        }
        valid, reasons = self.filler.validate_before_submit(result)
        self.assertFalse(valid)
        self.assertTrue(any("CAPTCHA" in r for r in reasons))

    def test_verification_pending_fails(self):
        result = {
            "fields_filled": {"email": True, "first_name": True},
            "resume_attached": True,
            "captcha_detected": False,
            "verification_pending": True,
        }
        valid, reasons = self.filler.validate_before_submit(result)
        self.assertFalse(valid)
        self.assertTrue(any("Verification code" in r for r in reasons))

    def test_all_requirements_met_passes(self):
        result = {
            "fields_filled": {"email": True, "first_name": True},
            "resume_attached": True,
            "captcha_detected": False,
            "verification_pending": False,
        }
        valid, reasons = self.filler.validate_before_submit(result)
        self.assertTrue(valid)
        self.assertEqual(len(reasons), 0)


if __name__ == "__main__":
    unittest.main()
