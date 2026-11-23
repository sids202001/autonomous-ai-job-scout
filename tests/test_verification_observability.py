"""
Unit tests for Job Scout - Application Verification & Observability Fix Pack:
1. FIX 1: Terminal outcome for every launch & STALLED flag after 30 min.
2. FIX 2: 4-evidence item requirement (log, screenshot, Telegram receipt msg_id, digest entry). Missing any -> UNVERIFIED.
3. FIX 3: Digest outcome ledger rendering and accessibility.
4. FIX 4: Build proof tracking (2026-09-20.v2.observability).
"""

import os
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from app.services.launch_tracker import LaunchTracker, CURRENT_BUILD_ID
from app.services.audit_logger import AuditLogger


class TestVerificationAndObservability(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tracker_path = os.path.join(self.temp_dir.name, "launch_tracker.json")
        self.proof_path = os.path.join(self.temp_dir.name, "build_proof.json")
        self.audit_log_path = os.path.join(self.temp_dir.name, "submissions.log")
        self.tracker = LaunchTracker(tracker_path=self.tracker_path, proof_path=self.proof_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_fix1_launch_registration_and_terminal_outcomes(self):
        """Every launch begins as LAUNCHED and resolves to a terminal outcome."""
        l_id = self.tracker.register_launch(
            company="TestCo",
            role="Backend Engineer",
            url="https://boards.greenhouse.io/testco/jobs/123",
            portal="greenhouse"
        )
        self.assertTrue(l_id.startswith("lnch_"))
        
        # Verify initial state
        records = self.tracker._read_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "LAUNCHED")
        self.assertIsNone(records[0]["terminal_outcome"])

        # Resolve to terminal outcome
        resolved = self.tracker.resolve_launch(
            launch_id=l_id,
            status="FAILED",
            reason="Submit button not found",
            sync_audit_log=False
        )
        self.assertEqual(resolved["status"], "FAILED")
        self.assertEqual(resolved["terminal_outcome"], "FAILED")
        self.assertEqual(resolved["reason"], "Submit button not found")

    def test_fix1_stalled_launches_flagged_after_30_minutes(self):
        """Launches hanging without terminal outcome for >30m are flagged STALLED in digest."""
        records = [{
            "launch_id": "lnch_old",
            "timestamp": (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
            "company": "SlowCo",
            "role": "Data Engineer",
            "url": "https://jobs.lever.co/slowco/456",
            "portal": "lever",
            "status": "LAUNCHED",
            "terminal_outcome": None,
            "resolved_at": None,
            "reason": None,
            "screenshot_path": None,
            "receipt_msg_id": None,
            "evidence": {}
        }]
        self.tracker._write_records(records)

        stalled = self.tracker.check_stalled_launches(timeout_minutes=30)
        self.assertEqual(len(stalled), 1)
        self.assertEqual(stalled[0]["status"], "STALLED")
        self.assertEqual(stalled[0]["terminal_outcome"], "STALLED")
        self.assertIn("exceeded 30m", stalled[0]["reason"])

        # Fresh launch (<30m) should not be flagged stalled
        l2 = self.tracker.register_launch("FreshCo", "SWE", "https://boards.greenhouse.io/freshco/1")
        stalled_again = self.tracker.check_stalled_launches(timeout_minutes=30)
        self.assertEqual(len(stalled_again), 0)

    def test_fix2_submitted_requires_all_four_evidence_items(self):
        """Missing any of the 4 evidence items results in UNVERIFIED, never SUBMITTED."""
        # 1. Missing screenshot
        ok1, reason1 = self.tracker.verify_submission_evidence(
            log_entry=True,
            screenshot_path=None,
            receipt_msg_id=12345,
            digest_entry=True
        )
        self.assertFalse(ok1)
        self.assertIn("confirmation screenshot", reason1)

        # 2. Non-existent screenshot file
        ok2, reason2 = self.tracker.verify_submission_evidence(
            log_entry=True,
            screenshot_path="/nonexistent/path/shot.png",
            receipt_msg_id=12345,
            digest_entry=True
        )
        self.assertFalse(ok2)
        self.assertIn("confirmation screenshot", reason2)

        # 3. Missing Telegram receipt msg_id
        dummy_ss = os.path.join(self.temp_dir.name, "confirm.png")
        with open(dummy_ss, "wb") as f:
            f.write(b"PNGDATA")
        ok3, reason3 = self.tracker.verify_submission_evidence(
            log_entry=True,
            screenshot_path=dummy_ss,
            receipt_msg_id=None,
            digest_entry=True
        )
        self.assertFalse(ok3)
        self.assertIn("Telegram success receipt", reason3)

        # 4. Missing log entry
        ok4, reason4 = self.tracker.verify_submission_evidence(
            log_entry=False,
            screenshot_path=dummy_ss,
            receipt_msg_id=99999,
            digest_entry=True
        )
        self.assertFalse(ok4)
        self.assertIn("log entry", reason4)

        # 5. All 4 items present -> Passes
        ok5, reason5 = self.tracker.verify_submission_evidence(
            log_entry=True,
            screenshot_path=dummy_ss,
            receipt_msg_id=99999,
            digest_entry=True
        )
        self.assertTrue(ok5)
        self.assertIn("All 4 evidence items verified", reason5)

    def test_fix2_resolve_launch_downgrades_to_unverified_if_evidence_missing(self):
        """Resolving with SUBMITTED status without screenshot or receipt downgrades to UNVERIFIED."""
        l_id = self.tracker.register_launch("AuditCo", "SWE", "https://boards.greenhouse.io/auditco/1")
        resolved = self.tracker.resolve_launch(
            launch_id=l_id,
            status="SUBMITTED",
            screenshot_path=None,
            receipt_msg_id=None,
            sync_audit_log=False
        )
        self.assertEqual(resolved["status"], "UNVERIFIED")
        self.assertEqual(resolved["terminal_outcome"], "UNVERIFIED")
        self.assertIn("UNVERIFIED:", resolved["reason"])

    def test_fix3_outcome_ledger_rendering_in_digest(self):
        """Every launch is rendered in the digest outcome ledger."""
        dummy_ss = os.path.join(self.temp_dir.name, "shot.png")
        with open(dummy_ss, "wb") as f:
            f.write(b"DATA")

        l_id = self.tracker.register_launch("LedgerCo", "Platform Engineer", "https://boards.greenhouse.io/ledgerco/1")
        self.tracker.resolve_launch(
            launch_id=l_id,
            status="SUBMITTED",
            reason="Confirmed submitted",
            screenshot_path=dummy_ss,
            receipt_msg_id=77777,
            sync_audit_log=False
        )

        with patch("app.services.launch_tracker.launch_tracker", self.tracker):
            logger_inst = AuditLogger()
            digest = logger_inst.generate_run_digest(window_minutes=1440)
            self.assertIn("Outcome Ledger (Last 24h Launches & Outcomes)", digest)
            self.assertIn("LedgerCo", digest)
            self.assertIn("Platform Engineer", digest)
            self.assertIn("SUBMITTED", digest)
            self.assertIn("shot.png", digest)
            self.assertIn("Receipt: 77777", digest)
            self.assertIn("Live Digest & Audit Access:", digest)

    def test_fix4_build_proof_management(self):
        """Build begins unproven and is proven when full evidence submission occurs."""
        self.assertFalse(self.tracker.is_build_proven())
        status = self.tracker.get_build_proof_status()
        self.assertEqual(status["current_build_id"], CURRENT_BUILD_ID)
        self.assertFalse(status["proven"])

        dummy_ss = os.path.join(self.temp_dir.name, "proof_shot.png")
        with open(dummy_ss, "wb") as f:
            f.write(b"PROOFPNG")

        l_id = self.tracker.register_launch("ProofCorp", "SWE", "https://boards.greenhouse.io/proof/1")
        self.tracker.resolve_launch(
            launch_id=l_id,
            status="SUBMITTED",
            screenshot_path=dummy_ss,
            receipt_msg_id=88888,
            sync_audit_log=False
        )

        self.assertTrue(self.tracker.is_build_proven())
        status_proven = self.tracker.get_build_proof_status()
        self.assertTrue(status_proven["proven"])
        self.assertEqual(status_proven["proof_record"]["company"], "ProofCorp")
        self.assertEqual(status_proven["proof_record"]["receipt_msg_id"], "88888")


if __name__ == "__main__":
    unittest.main()
