"""
Unit & regression tests for DIGEST EVIDENCE ATTACHMENTS (Telegram /digest UX):
1. Verifies photos attach per outcome-ledger entry that has a screenshot file on disk.
2. Verifies photo captions strictly follow 'Company — Role | timestamp | outcome'.
3. Verifies missing files / 0-byte files are skipped silently without error spam.
4. Verifies 10-cap rule holds when >10 screenshots exist, selecting the 10 most recent.
5. Verifies overflow note '+N more on disk: ~/Desktop/screenshots/' is sent when >10 screenshots exist.
6. Verifies mocked Telegram send (reply_media_group, reply_photo, reply_text) end-to-end.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure sid_job_scrip and Desktop are in path
sys.path.insert(0, "/Users/siddheshsawant/sid_job_scrip")
sys.path.insert(0, "/Users/siddheshsawant/Desktop")

from app.services.audit_logger import collect_digest_evidence_photos
from resume_bot import handle_digest


class TestDigestEvidenceAttachments(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _create_dummy_image(self, name: str) -> str:
        """Create a dummy non-empty file on disk to simulate a screenshot."""
        path = os.path.join(self.temp_dir.name, name)
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRdummy_screenshot_data")
        return path

    def test_collect_digest_evidence_photos_captions_and_missing_files(self):
        """
        Verify:
        - Captions strictly follow 'Company — Role | timestamp | outcome'
        - Missing file paths and 0-byte files are skipped silently (no errors)
        """
        img1 = self._create_dummy_image("Jobgether_20260925_232621.png")
        img2 = self._create_dummy_image("Datalabusa_20260926_093733.png")
        empty_img = os.path.join(self.temp_dir.name, "empty.png")
        with open(empty_img, "wb") as f:
            pass  # 0 bytes

        mock_launches = [
            {
                "company": "Jobgether",
                "role": "MLOps Field Engineer",
                "status": "CHALLENGE_BLOCKED",
                "timestamp": "2026-09-25T23:26:21.123456",
                "screenshot_path": img1,
            },
            {
                "company": "MissingCorp",
                "role": "Software Engineer",
                "status": "FAILED",
                "timestamp": "2026-09-26T08:00:00.000000",
                "screenshot_path": "/nonexistent/path/screenshot.png",  # missing file
            },
            {
                "company": "EmptyCorp",
                "role": "Data Analyst",
                "status": "FAILED",
                "timestamp": "2026-09-26T08:30:00.000000",
                "screenshot_path": empty_img,  # 0-byte file
            },
            {
                "company": "Datalabusa",
                "role": "Production Programmer - Entry Level SQL Developer",
                "status": "SUBMITTED",
                "timestamp": "2026-09-26T09:37:33.999999",
                "screenshot_path": img2,
            },
        ]

        photos, overflow, overflow_line = collect_digest_evidence_photos(mock_launches)

        # MissingCorp and EmptyCorp should be skipped silently
        self.assertEqual(len(photos), 2)
        self.assertEqual(overflow, 0)
        self.assertIsNone(overflow_line)

        # Verify captions
        self.assertEqual(
            photos[0]["caption"],
            "Jobgether — MLOps Field Engineer | 2026-09-25 23:26:21 | CHALLENGE_BLOCKED"
        )
        self.assertEqual(
            photos[1]["caption"],
            "Datalabusa — Production Programmer - Entry Level SQL Developer | 2026-09-26 09:37:33 | SUBMITTED"
        )

    def test_collect_digest_evidence_photos_10_cap_and_overflow(self):
        """
        Verify:
        - If >10 screenshots exist, only the 10 most recent are attached
        - Overflow count N is calculated accurately
        - Overflow line is formatted exactly: '+N more on disk: ~/Desktop/screenshots/'
        """
        mock_launches = []
        # Create 16 dummy launches with screenshots on disk
        for i in range(1, 17):
            img_path = self._create_dummy_image(f"screenshot_{i:02d}.png")
            mock_launches.append({
                "company": f"Company{i}",
                "role": f"Role{i}",
                "status": "CHALLENGE_BLOCKED" if i % 2 == 0 else "SUBMITTED",
                "timestamp": f"2026-09-26T10:{i:02d}:00.000000",
                "screenshot_path": img_path,
            })

        photos, overflow, overflow_line = collect_digest_evidence_photos(mock_launches)

        # Total is 16 -> 10 attached, 6 overflow
        self.assertEqual(len(photos), 10)
        self.assertEqual(overflow, 6)
        self.assertEqual(overflow_line, "+6 more on disk: ~/Desktop/screenshots/")

        # Verify the 10 attached are the most recent (Company7 to Company16)
        self.assertEqual(photos[0]["company"], "Company7")
        self.assertEqual(photos[-1]["company"], "Company16")
        self.assertEqual(
            photos[-1]["caption"],
            "Company16 — Role16 | 2026-09-26 10:16:00 | CHALLENGE_BLOCKED"
        )

    async def test_handle_digest_mocked_telegram_media_group_send(self):
        """
        Verify handle_digest sends digest text and attaches photos as media group with 10-cap held.
        """
        mock_launches = []
        for i in range(1, 14):  # 13 launches -> 10 attached, 3 overflow
            img_path = self._create_dummy_image(f"app_{i:02d}.png")
            mock_launches.append({
                "company": f"Corp{i}",
                "role": f"Dev{i}",
                "status": "SUBMITTED",
                "timestamp": f"2026-09-26T12:{i:02d}:00.000000",
                "screenshot_path": img_path,
            })

        update = MagicMock()
        update.effective_message = AsyncMock()
        update.message = update.effective_message
        update.effective_chat = MagicMock()
        update.effective_chat.id = 987654

        context = MagicMock()
        context.args = ["verbose"]

        with patch("app.services.launch_tracker.launch_tracker.get_last_24h_launches", return_value=mock_launches), \
             patch("app.services.intelligence_loop.outcome_ledger.get_last_24h_outcomes", return_value=[]):
            await handle_digest(update, context)

            # 1. Digest text sent
            update.effective_message.reply_text.assert_any_call(unittest.mock.ANY)

            # 2. Media group sent with 10 photos
            update.effective_message.reply_media_group.assert_called_once()
            media_group = update.effective_message.reply_media_group.call_args[1]["media"]
            self.assertEqual(len(media_group), 10)

            # Verify caption of first and last item in media group
            self.assertEqual(media_group[0].caption, "Corp4 — Dev4 | 2026-09-26 12:04:00 | SUBMITTED")
            self.assertEqual(media_group[-1].caption, "Corp13 — Dev13 | 2026-09-26 12:13:00 | SUBMITTED")

            # 3. Overflow note sent (+3 more on disk: ~/Desktop/screenshots/)
            update.effective_message.reply_text.assert_any_call("+3 more on disk: ~/Desktop/screenshots/")

    async def test_handle_digest_single_photo_fallback(self):
        """
        Verify handle_digest sends a single photo via reply_photo when exactly 1 screenshot exists.
        (Telegram media groups require >= 2 items).
        """
        img_path = self._create_dummy_image("single.png")
        mock_launches = [
            {
                "company": "SoloCorp",
                "role": "Staff Engineer",
                "status": "SUBMITTED",
                "timestamp": "2026-09-26T15:30:00.000000",
                "screenshot_path": img_path,
            }
        ]

        update = MagicMock()
        update.effective_message = AsyncMock()
        update.message = update.effective_message
        update.effective_chat = MagicMock()
        update.effective_chat.id = 987654

        context = MagicMock()
        context.args = ["verbose"]

        with patch("app.services.launch_tracker.launch_tracker.get_last_24h_launches", return_value=mock_launches), \
             patch("app.services.intelligence_loop.outcome_ledger.get_last_24h_outcomes", return_value=[]):
            await handle_digest(update, context)

            # reply_photo called with caption
            update.effective_message.reply_photo.assert_called_once()
            call_kwargs = update.effective_message.reply_photo.call_args[1]
            self.assertEqual(call_kwargs["caption"], "SoloCorp — Staff Engineer | 2026-09-26 15:30:00 | SUBMITTED")
            # reply_media_group should not be called
            update.effective_message.reply_media_group.assert_not_called()

    async def test_handle_digest_no_photos_silent(self):
        """
        Verify handle_digest sends digest text and does not attempt media attachments when no screenshots exist.
        """
        mock_launches = [
            {
                "company": "TextOnlyCorp",
                "role": "Lead Architect",
                "status": "SKIPPED",
                "timestamp": "2026-09-26T16:00:00.000000",
                "screenshot_path": None,
            }
        ]

        update = MagicMock()
        update.effective_message = AsyncMock()
        update.message = update.effective_message
        update.effective_chat = MagicMock()
        update.effective_chat.id = 987654

        context = MagicMock()

        with patch("app.services.launch_tracker.launch_tracker.get_last_24h_launches", return_value=mock_launches), \
             patch("app.services.intelligence_loop.outcome_ledger.get_last_24h_outcomes", return_value=[]):
            await handle_digest(update, context)

            self.assertTrue(update.effective_message.reply_text.called)
            update.effective_message.reply_media_group.assert_not_called()
            update.effective_message.reply_photo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
