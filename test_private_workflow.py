"""Private-chat video routing tests (no network/Gemini calls)."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from storage import Storage
from tracker_core import setup_manual_storage
from video_workflow import VideoWorkflow


class Allowlist:
    def __init__(self, users=(), groups=()):
        self.users = set(users)
        self.groups = set(groups)

    def allowed(self, kind, identifier):
        return identifier in (self.users if kind == "user" else self.groups)


class PrivateWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(str(Path(self.tmp.name) / "pushups.sqlite"))
        setup_manual_storage(self.db)
        self.access = Allowlist(users={123}, groups={-100})
        self.workflow = VideoWorkflow(
            None, self.db, self.access, ZoneInfo("UTC"),
            asyncio.Semaphore(1),
        )
        self.workflow.refresh_widget = AsyncMock()
        self.workflow._process = AsyncMock()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    @staticmethod
    def message(chat_id, chat_type, user_id, message_id=50):
        return SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type=chat_type, title=None),
            from_user=SimpleNamespace(id=user_id, is_bot=False, full_name="Tester"),
            video_note=SimpleNamespace(file_id="video-id", duration=15, file_size=100),
            date=datetime(2026, 11, 21, tzinfo=timezone.utc),
            message_thread_id=None,
            message_id=message_id,
        )

    def test_private_circle_uses_same_persistent_workflow(self):
        asyncio.run(self.workflow.on_video(self.message(123, "private", 123)))
        job = self.db.get_video_job(123, 50)
        self.assertIsNotNone(job)
        self.assertEqual(job.state, "queued")
        self.assertEqual(job.user_id, 123)
        self.assertTrue(self.workflow.job_allowed(job))
        self.assertEqual(self.db.chat_ids(), [])  # Личка не участвует в групповых cron-отчётах.
        self.workflow.refresh_widget.assert_awaited_once_with(123, 50)
        self.workflow._process.assert_awaited_once_with(123, 50)

    def test_private_circle_requires_user_allowlist(self):
        asyncio.run(self.workflow.on_video(self.message(456, "private", 456)))
        self.assertIsNone(self.db.get_video_job(456, 50))
        self.workflow._process.assert_not_awaited()

    def test_group_flow_is_unchanged(self):
        asyncio.run(self.workflow.on_video(self.message(-100, "supergroup", 123)))
        self.assertTrue(self.workflow.job_allowed(self.db.get_video_job(-100, 50)))
        self.workflow._process.assert_awaited_once_with(-100, 50)

    def test_private_callbacks_use_user_permission(self):
        self.assertTrue(self.workflow.chat_allowed("private", 123, 123))
        self.assertFalse(self.workflow.chat_allowed("private", 456, 456))
        self.assertTrue(self.workflow.chat_allowed("supergroup", -100, 123))
        self.assertFalse(self.workflow.chat_allowed("supergroup", -200, 123))


if __name__ == "__main__":
    unittest.main()
