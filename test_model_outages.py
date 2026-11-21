"""Retry credit protection and Gemini model telemetry regression tests."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from storage import Storage
from tracker_core import setup_manual_storage
from video_workflow import VideoWorkflow
from vision import (
    Analysis, INITIAL_MODEL_CHAIN, RETRY_MODEL_CHAIN,
    ModelOverloadedError, PRIMARY_MODEL, SECONDARY_MODEL, WEAK_MODEL,
)


class AllowAll:
    def allowed(self, kind, identifier):
        return True


class OutageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(str(Path(self.tmp.name) / "db.sqlite"))
        setup_manual_storage(self.db)
        self.db.create_video_job(
            123, 50, 123, "Tester", "file-id", 15, 100,
            datetime.now(timezone.utc),
        )
        bot = AsyncMock()

        async def download(*args, destination, **kwargs):
            Path(destination).write_bytes(b"video")

        bot.download.side_effect = download
        self.flow = VideoWorkflow(
            bot, self.db, AllowAll(), ZoneInfo("UTC"), asyncio.Semaphore(1),
        )
        self.flow.refresh_widget = AsyncMock()
        self.calls = []

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def analyzer(self, values):
        def call(*args, **kwargs):
            model = kwargs["model_override"]
            self.calls.append(model)
            value = values[model]
            if isinstance(value, BaseException):
                raise value
            return value

        return call

    async def test_outage_refunds_retry_and_keeps_previous_count(self):
        self.db.complete_video_analysis(123, 50, 20, "weak")
        self.assertTrue(self.db.reserve_video_retry(123, 50))
        with patch("video_workflow.analyze_video", self.analyzer({
            PRIMARY_MODEL: ModelOverloadedError(PRIMARY_MODEL, 504),
            SECONDARY_MODEL: ModelOverloadedError(SECONDARY_MODEL, 503),
        })):
            await self.flow._process(123, 50)
        job = self.db.get_video_job(123, 50)
        self.assertEqual(self.calls, list(RETRY_MODEL_CHAIN))
        self.assertEqual(job.retry_used, 0)
        self.assertEqual(job.state, "done")
        self.assertEqual(self.db.get_video_result(123, 50), (20, False))
        self.assertFalse(job.result_locked)
        counts = self.db.model_request_stats()
        self.assertEqual(counts[PRIMARY_MODEL]["day"][3], 1)
        self.assertEqual(counts[SECONDARY_MODEL]["day"][3], 1)

    async def test_uncertain_response_does_use_credit(self):
        self.db.complete_video_analysis(123, 50, 20, "weak")
        self.assertTrue(self.db.reserve_video_retry(123, 50))
        with patch("video_workflow.analyze_video", self.analyzer({
            PRIMARY_MODEL: Analysis(repetitions=5, uncertain=True),
            SECONDARY_MODEL: ModelOverloadedError(SECONDARY_MODEL, 504),
        })):
            await self.flow._process(123, 50)
        self.assertEqual(self.db.get_video_job(123, 50).retry_used, 1)

    async def test_429_is_counted_as_quota_error_not_success(self):
        with patch("video_workflow.analyze_video", self.analyzer({
            PRIMARY_MODEL: ModelOverloadedError(PRIMARY_MODEL, 429),
            SECONDARY_MODEL: ModelOverloadedError(SECONDARY_MODEL, 503),
            WEAK_MODEL: ModelOverloadedError(WEAK_MODEL, 504),
        })):
            await self.flow._process(123, 50)
        stats = self.db.model_request_stats()
        self.assertEqual(stats[PRIMARY_MODEL]["day"][2], 1)
        self.assertEqual(stats[SECONDARY_MODEL]["day"][3], 1)
        self.assertEqual(stats[WEAK_MODEL]["day"][3], 1)
        self.assertEqual(self.db.get_video_job(123, 50).retry_used, 0)

    async def test_token_counters_per_model(self):
        with patch("video_workflow.analyze_video", self.analyzer({
            PRIMARY_MODEL: Analysis(
                repetitions=12,
                usage_metadata={
                    "prompt_token_count": 1000,
                    "candidates_token_count": 20,
                    "total_token_count": 1020,
                },
            ),
        })):
            await self.flow._process(123, 50)
        data = self.db.model_request_stats()[PRIMARY_MODEL]["day"]
        self.assertEqual(data[0], 1)
        self.assertEqual(data[1], 1)
        self.assertEqual(data[4:], (1000, 20, 1020))


if __name__ == "__main__":
    unittest.main()
