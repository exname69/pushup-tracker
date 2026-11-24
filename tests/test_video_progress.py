"""Regression coverage for the progress card and the first-attempt gate."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from rich_messages import processing_bar, render_video_card
from storage import Storage
from tracker_core import create_pending_manual, process_manual_reply, setup_manual_storage
from video_workflow import VideoWorkflow


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.db = Storage(":memory:")
        setup_manual_storage(self.db)
        self.now = datetime.now(timezone.utc)
        self.key = (-100, 42)
        self.db.create_video_job(*self.key, 7, "Test", "file", 20, 100, self.now)

    def tearDown(self):
        self.db.conn.close()

    def card(self, frame=0):
        return render_video_card(
            self.db.get_video_job(*self.key), self.db.get_video_result(*self.key),
            ZoneInfo("UTC"), now=self.now, progress_frame=frame,
        )

    def test_indicator_moves_and_keeps_width(self):
        frames = [processing_bar(i) for i in range(15)]
        self.assertNotEqual(frames[0], frames[1])
        self.assertEqual(frames[0], frames[-1])
        for frame in frames:
            self.assertEqual(len(frame), 10)
            self.assertEqual(frame.count("▰"), 3)

    def test_first_failure_stays_retryable_without_deadline(self):
        for state in ("queued", "downloading", "analyzing"):
            self.db.set_video_job_state(*self.key, state)
            content, actions = self.card()
            self.assertFalse(actions)
            self.assertNotIn("video:manual:", content.html)
        self.db.finish_video_job(*self.key, "failed", now=self.now)
        job = self.db.get_video_job(*self.key)
        self.assertEqual(job.completed_attempts, 1)
        self.assertIsNone(job.action_expires_at_utc)
        content, actions = render_video_card(
            job, None, ZoneInfo("UTC"), now=self.now + timedelta(days=1),
        )
        self.assertTrue(actions)
        self.assertIn("video:retry:", content.html)
        self.assertNotIn("video:manual:", content.html)
        self.assertNotIn("Доступно до", content.html)
        self.db.set_video_job_state(*self.key, "queued")
        self.assertNotIn("video:manual:", self.card()[0].html)

    def test_second_failure_starts_window_and_allows_manual(self):
        self.db.finish_video_job(*self.key, "failed", now=self.now)
        later = self.now + timedelta(days=1)
        self.db.set_video_job_state(*self.key, "analyzing")
        self.db.finish_video_job(*self.key, "failed", now=later)
        job = self.db.get_video_job(*self.key)
        deadline = later + timedelta(minutes=15)
        self.assertEqual(job.completed_attempts, 2)
        self.assertEqual(datetime.fromisoformat(job.action_expires_at_utc), deadline)
        content, actions = render_video_card(job, None, ZoneInfo("UTC"), now=later)
        self.assertTrue(actions)
        self.assertIn("video:manual:", content.html)
        create_pending_manual(self.db, *self.key, 7, "Test", self.now, created_at=later)
        self.assertEqual(process_manual_reply(self.db, *self.key, 7, "12", now=later).status, "accepted")
        self.assertEqual(self.db.get_video_job(*self.key).completed_attempts, 2)
        self.assertEqual(self.db.get_video_job(*self.key).action_expires_at_utc, deadline.isoformat())

    def test_first_success_starts_window_but_manual_requires_retry(self):
        self.db.replace_video_result(*self.key, 12)
        self.db.finish_video_job(*self.key, "done", now=self.now)
        content, actions = self.card()
        self.assertTrue(actions)
        self.assertNotIn("video:manual:", content.html)
        self.assertIn("video:retry:", content.html)
        self.assertIn("Результат: 12", content.html)
        deadline = self.db.get_video_job(*self.key).action_expires_at_utc
        self.assertEqual(datetime.fromisoformat(deadline), self.now + timedelta(minutes=15))
        create_pending_manual(self.db, *self.key, 7, "Test", self.now, created_at=self.now)
        self.assertEqual(process_manual_reply(self.db, *self.key, 7, "13", now=self.now).status, "busy")
        self.db.set_video_job_state(*self.key, "analyzing")
        self.db.finish_video_job(*self.key, "done", now=self.now + timedelta(minutes=5))
        self.assertIn("video:manual:", self.card()[0].html)
        self.assertEqual(self.db.get_video_job(*self.key).action_expires_at_utc, deadline)

    def test_restart_does_not_count_as_attempt(self):
        self.db.finish_video_job(*self.key, "failed", now=self.now, attempt_completed=False)
        job = self.db.get_video_job(*self.key)
        self.assertEqual(job.completed_attempts, 0)
        self.assertIsNone(job.action_expires_at_utc)
        self.assertIn("video:retry:", self.card()[0].html)
        self.assertNotIn("video:manual:", self.card()[0].html)
        self.db.finish_video_job(*self.key, "failed", now=self.now)
        self.db.set_video_job_state(*self.key, "analyzing")
        self.db.finish_video_job(*self.key, "failed", now=self.now, attempt_completed=False)
        self.assertEqual(self.db.get_video_job(*self.key).completed_attempts, 1)
        self.assertIsNone(self.db.get_video_job(*self.key).action_expires_at_utc)

    def test_expired_card_only_shows_count_and_rejects_manual_reply(self):
        self.db.replace_video_result(*self.key, 12)
        self.db.finish_video_job(*self.key, "done", now=self.now)
        self.db.finish_video_job(*self.key, "done", now=self.now)
        create_pending_manual(self.db, *self.key, 7, "Test", self.now, created_at=self.now)
        deadline = self.now + timedelta(minutes=15)
        job = self.db.get_video_job(*self.key)
        content, actions = render_video_card(job, (12, False), ZoneInfo("UTC"), now=deadline)
        self.assertFalse(actions)
        self.assertEqual(content.html, "<p>💪 Отжимания: <strong>12</strong></p>")
        self.assertEqual(process_manual_reply(self.db, *self.key, 7, "13", now=deadline).status, "expired")
        self.assertEqual(self.db.get_video_result(*self.key), (12, False))

    def test_expired_failure_has_no_invented_zero(self):
        self.db.finish_video_job(*self.key, "failed", now=self.now)
        self.db.finish_video_job(*self.key, "failed", now=self.now)
        content, actions = render_video_card(
            self.db.get_video_job(*self.key), None, ZoneInfo("UTC"),
            now=self.now + timedelta(minutes=15),
        )
        self.assertFalse(actions)
        self.assertEqual(content.html, "<p>💪 Отжимания: не засчитано</p>")

    def test_expired_retry_card_shows_only_previous_count(self):
        self.db.replace_video_result(*self.key, 12)
        self.db.finish_video_job(*self.key, "done", now=self.now)
        self.db.set_video_job_state(*self.key, "analyzing")
        content, actions = render_video_card(
            self.db.get_video_job(*self.key), (12, True), ZoneInfo("UTC"),
            note="fallback", now=self.now + timedelta(minutes=15),
        )
        self.assertFalse(actions)
        self.assertEqual(content.html, "<p>💪 Отжимания: <strong>12</strong></p>")

    def test_animation_is_cancelled_before_final_refresh(self):
        async def run():
            workflow = VideoWorkflow(
                None, self.db, None, ZoneInfo("UTC"), asyncio.Semaphore(1), "fallback",
            )
            started = asyncio.Event()
            stopped = asyncio.Event()

            async def animate(*args):
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()

            async def process(*args, **kwargs):
                await started.wait()

            async def refresh(*args, **kwargs):
                self.assertTrue(stopped.is_set())

            workflow._animate_widget = animate
            workflow._run_processing = process
            workflow.refresh_widget = AsyncMock(side_effect=refresh)
            await workflow._process(*self.key)
            workflow.refresh_widget.assert_awaited_once_with(*self.key)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
