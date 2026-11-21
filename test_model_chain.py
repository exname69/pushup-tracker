"""Regression tests for three-stage Gemini model selection."""
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
    Analysis, PRIMARY_MODEL, SECONDARY_MODEL, WEAK_MODEL,
    INITIAL_MODEL_CHAIN, RETRY_MODEL_CHAIN, ModelOverloadedError,
)


class AllowAll:
    def allowed(self, kind, identifier):
        return True


class ModelChainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(str(Path(self.tmp.name) / "test.sqlite3"))
        setup_manual_storage(self.db)
        self.db.create_video_job(
            123, 50, 123, "Tester", "file-id", 15, 100,
            datetime.now(timezone.utc),
        )
        bot = AsyncMock()

        async def download(*args, destination, **kwargs):
            Path(destination).write_bytes(b"video")

        bot.download.side_effect = download
        self.workflow = VideoWorkflow(
            bot, self.db, AllowAll(), ZoneInfo("UTC"), asyncio.Semaphore(1),
        )
        self.workflow.refresh_widget = AsyncMock()
        self.called = []

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def fake_analyzer(self, responses):
        def analyze(*args, **kwargs):
            model = kwargs["model_override"]
            self.called.append(model)
            reply = responses.get(model)
            if isinstance(reply, Exception):
                raise reply
            return reply

        return analyze

    async def test_model_order_is_fixed_in_python(self):
        self.assertEqual(
            INITIAL_MODEL_CHAIN, (PRIMARY_MODEL, SECONDARY_MODEL, WEAK_MODEL),
        )
        self.assertEqual(RETRY_MODEL_CHAIN, (PRIMARY_MODEL, SECONDARY_MODEL))

    async def test_primary_success_stops_chain(self):
        with patch("video_workflow.analyze_video", self.fake_analyzer({
            PRIMARY_MODEL: Analysis(repetitions=21),
        })):
            await self.workflow._process(123, 50)
        self.assertEqual(self.called, [PRIMARY_MODEL])
        self.assertEqual(self.db.get_video_result(123, 50), (21, False))
        self.assertEqual(self.db.get_video_job(123, 50).result_source, "strong")
        self.assertEqual(self.db.get_video_job(123, 50).retry_limit, 1)

    async def test_hydrogen_success_stops_before_weak(self):
        with patch("video_workflow.analyze_video", self.fake_analyzer({
            PRIMARY_MODEL: None,
            SECONDARY_MODEL: Analysis(repetitions=24),
        })):
            await self.workflow._process(123, 50)
        self.assertEqual(self.called, [PRIMARY_MODEL, SECONDARY_MODEL])
        self.assertEqual(self.db.get_video_result(123, 50), (24, False))
        self.assertEqual(self.db.get_video_job(123, 50).result_source, "strong")

    async def test_weak_used_after_both_strong_models_fail(self):
        with patch("video_workflow.analyze_video", self.fake_analyzer({
            PRIMARY_MODEL: Analysis(repetitions=8, uncertain=True),
            SECONDARY_MODEL: ModelOverloadedError("overloaded"),
            WEAK_MODEL: Analysis(repetitions=23),
        })):
            await self.workflow._process(123, 50)
        self.assertEqual(self.called, list(INITIAL_MODEL_CHAIN))
        self.assertEqual(self.db.get_video_result(123, 50), (23, False))
        self.assertEqual(self.db.get_video_job(123, 50).result_source, "weak")
        self.assertEqual(self.db.get_video_job(123, 50).retry_limit, 2)

    async def test_retry_does_not_call_weak_model(self):
        with patch("video_workflow.analyze_video", self.fake_analyzer({
            PRIMARY_MODEL: None,
            SECONDARY_MODEL: None,
            WEAK_MODEL: Analysis(repetitions=22),
        })):
            await self.workflow._process(123, 50)

        self.assertTrue(self.db.reserve_video_retry(123, 50))
        self.called.clear()
        with patch("video_workflow.analyze_video", self.fake_analyzer({
            PRIMARY_MODEL: None,
            SECONDARY_MODEL: Analysis(repetitions=25),
            WEAK_MODEL: Analysis(repetitions=99),
        })):
            await self.workflow._process(123, 50)
        self.assertEqual(self.called, list(RETRY_MODEL_CHAIN))
        self.assertEqual(self.db.get_video_result(123, 50), (25, False))
        self.assertTrue(self.db.get_video_job(123, 50).result_locked)

    async def test_all_models_fail(self):
        with patch("video_workflow.analyze_video", self.fake_analyzer({
            PRIMARY_MODEL: None,
            SECONDARY_MODEL: None,
            WEAK_MODEL: None,
        })):
            await self.workflow._process(123, 50)
        self.assertEqual(self.called, list(INITIAL_MODEL_CHAIN))
        self.assertEqual(self.db.get_video_job(123, 50).state, "failed")
        self.assertIsNone(self.db.get_video_result(123, 50))


if __name__ == "__main__":
    unittest.main()
