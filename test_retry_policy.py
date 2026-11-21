"""Regression tests for push-up retry and result locking policy (stdlib unittest)."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from storage import Storage, video_action_flags
from tracker_core import (
    create_pending_manual, process_manual_reply, setup_manual_storage,
)


class RetryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(str(Path(self.tmp.name) / "test.sqlite3"))
        setup_manual_storage(self.db)
        self.started = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
        self.db.create_video_job(
            -100, 50, 123, "Tester", "file-id", 20, 1000, self.started,
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def job(self):
        return self.db.get_video_job(-100, 50)

    def done(self, count, source, at=None):
        self.db.complete_video_analysis(
            -100, 50, count, source, now=at or self.started,
        )

    def retry(self, at=None):
        return self.db.reserve_video_retry(-100, 50, now=at or self.started)

    def flags(self, at=None):
        return video_action_flags(self.job(), at or self.started)

    def test_strong_first_success_one_retry_and_five_minute_lock(self):
        self.done(25, "strong")
        self.assertEqual(self.job().retry_limit, 1)
        self.assertEqual(self.flags(self.started + timedelta(minutes=4)), (True, False))
        self.assertEqual(self.flags(self.started + timedelta(minutes=5)), (False, False))
        self.assertFalse(self.retry(self.started + timedelta(minutes=5)))
        self.db.lock_expired_video_jobs(self.started + timedelta(minutes=5))
        self.assertTrue(self.job().result_locked)

    def test_strong_retry_success_locks_and_updates_result(self):
        self.done(25, "strong")
        self.assertTrue(self.retry(self.started + timedelta(minutes=1)))
        self.assertFalse(self.retry(self.started + timedelta(minutes=1)))
        self.done(27, "strong", self.started + timedelta(minutes=2))
        self.assertEqual(self.db.get_video_result(-100, 50), (27, False))
        self.assertEqual(self.flags(self.started + timedelta(minutes=2)), (False, False))
        self.assertTrue(self.job().result_locked)

    def test_failed_strong_retry_preserves_previous_count_and_locks(self):
        self.done(25, "strong")
        self.assertTrue(self.retry(self.started + timedelta(minutes=1)))
        self.done(None, None, self.started + timedelta(minutes=2))
        self.assertEqual(self.db.get_video_result(-100, 50), (25, False))
        self.assertTrue(self.job().result_locked)

    def test_weak_fallback_grants_two_strong_retries_then_manual(self):
        self.done(23, "weak")
        self.assertEqual(self.flags(), (True, False))
        self.assertTrue(self.retry())
        self.done(None, None, self.started + timedelta(minutes=1))
        self.assertEqual(self.db.get_video_result(-100, 50), (23, False))
        self.assertEqual(self.flags(self.started + timedelta(minutes=1)), (True, False))
        self.assertTrue(self.retry(self.started + timedelta(minutes=2)))
        self.done(None, None, self.started + timedelta(minutes=3))
        self.assertEqual(self.flags(self.started + timedelta(minutes=3)), (False, True))
        self.assertEqual(self.db.get_video_result(-100, 50), (23, False))
        self.assertFalse(self.retry(self.started + timedelta(minutes=3)))

    def test_successful_strong_retry_after_weak_locks_early(self):
        self.done(23, "weak")
        self.assertTrue(self.retry())
        self.done(26, "strong", self.started + timedelta(minutes=1))
        self.assertTrue(self.job().result_locked)
        self.assertEqual(self.job().retry_used, 1)
        self.assertEqual(self.db.get_video_result(-100, 50), (26, False))

    def test_neither_model_success_two_retries_then_manual(self):
        self.done(None, None)
        self.assertEqual(self.flags(), (True, False))
        self.assertTrue(self.retry())
        self.done(None, None, self.started + timedelta(minutes=1))
        self.assertTrue(self.retry(self.started + timedelta(minutes=2)))
        self.done(None, None, self.started + timedelta(minutes=3))
        self.assertEqual(self.flags(self.started + timedelta(minutes=3)), (False, True))

    def test_manual_reply_requires_exhausted_retries_and_locks(self):
        self.done(23, "weak")
        self.assertTrue(create_pending_manual(
            self.db, -100, 50, 123, "Tester", self.started,
            created_at=self.started, expires_at=self.started + timedelta(minutes=30),
        ))
        blocked = process_manual_reply(
            self.db, -100, 50, 123, "24", now=self.started,
        )
        self.assertEqual(blocked.status, "locked")
        self.assertEqual(self.db.get_video_result(-100, 50), (23, False))
        for offset in (1, 2):
            self.assertTrue(self.retry(self.started + timedelta(minutes=offset)))
            self.done(None, None, self.started + timedelta(minutes=offset))
        self.assertEqual(self.flags(self.started + timedelta(minutes=3)), (False, True))
        self.assertTrue(create_pending_manual(
            self.db, -100, 50, 123, "Tester", self.started,
            created_at=self.started + timedelta(minutes=3),
            expires_at=self.started + timedelta(minutes=10),
        ))
        accepted = process_manual_reply(
            self.db, -100, 50, 123, "24", now=self.started + timedelta(minutes=4),
        )
        self.assertEqual(accepted.status, "accepted")
        self.assertEqual(self.db.get_video_result(-100, 50), (24, True))
        self.assertTrue(self.job().result_locked)


if __name__ == "__main__":
    unittest.main()
