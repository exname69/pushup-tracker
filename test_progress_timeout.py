"""Tests for indeterminate progress and bounded video processing."""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from rich_messages import render_video_card
from storage import Storage
from tracker_core import setup_manual_storage
from video_workflow import VideoWorkflow


class StubAccess:
    def allowed(self, kind, identifier):
        return True


class ProgressAndTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(str(Path(self.tmp.name) / "test.sqlite"))
        setup_manual_storage(self.db)
        self.db.create_video_job(
            123, 50, 123, "Tester", "file-id", 15, 100,
            datetime(2026, 11, 21, tzinfo=timezone.utc),
        )
        self.bot = AsyncMock()

        async def download(*args, destination, **kwargs):
            Path(destination).write_bytes(b"video")

        self.bot.download.side_effect = download
        self.workflow = VideoWorkflow(
            self.bot, self.db, StubAccess(), ZoneInfo("UTC"),
            asyncio.Semaphore(1), "gemini-fallback",
        )
        self.workflow.refresh_widget = AsyncMock()

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_animated_bar_indicates_elapsed_time_without_percent(self):
        job = self.db.get_video_job(123, 50)
        first, _ = render_video_card(
            job, None, ZoneInfo("UTC"), progress_tick=0, elapsed_seconds=17,
        )
        second, _ = render_video_card(
            job, None, ZoneInfo("UTC"), progress_tick=4, elapsed_seconds=65,
        )
        self.assertIn("0:17", first.html)
        self.assertIn("1:05", second.html)
        self.assertNotEqual(first.html, second.html)
        self.assertIn("<code>", first.html)
        self.assertNotIn("100%", first.html)
        self.db.complete_video_analysis(123, 50, count=None, error="timeout")
        final, _ = render_video_card(self.db.get_video_job(123, 50), None, ZoneInfo("UTC"))
        self.assertNotIn("<code>", final.html)

    async def test_processing_timeout_converts_stalled_model_into_failed_card(self):
        def slow_model(*args, **kwargs):
            time.sleep(0.15)
            return None

        with (
            patch("video_workflow.VIDEO_PROCESS_TIMEOUT_SECONDS", 0.03),
            patch("video_workflow.analyze_video", side_effect=slow_model),
        ):
            await self.workflow._process(123, 50)

        job = self.db.get_video_job(123, 50)
        self.assertEqual(job.state, "failed")
        self.assertIn("Время обработки истекло", job.error)
        self.assertIsNone(self.db.get_video_result(123, 50))
        self.assertEqual(job.retry_used, 0)
        self.workflow.refresh_widget.assert_awaited()

    async def test_fast_failure_has_no_progress_bar(self):
        self.db.conn.execute(
            "UPDATE video_jobs SET file_size=? WHERE chat_id=123 AND message_id=50",
            (30 * 1024 * 1024,),
        )
        await self.workflow._process(123, 50)
        job = self.db.get_video_job(123, 50)
        self.assertEqual(job.state, "failed")
        self.assertIn("20 МБ", job.error)
        final, _ = render_video_card(job, None, ZoneInfo("UTC"))
        self.assertNotIn("<code>", final.html)


if __name__ == "__main__":
    unittest.main()
