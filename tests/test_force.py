import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from storage import Storage
from tracker_core import setup_manual_storage, create_pending_manual, personal_totals


class ForceTests(unittest.TestCase):
    def setUp(self):
        self.db = Storage(":memory:")
        setup_manual_storage(self.db)
        self.now = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
        self.key = (-100, 42)
        self.db.create_video_job(*self.key, 7, "A", "file", 20, 100, self.now)

    def tearDown(self):
        self.db.close()

    def force(self, count):
        return self.db.force_video_result(*self.key, 7, count, 99)

    def test_replaces_result_and_locks_late_processing(self):
        self.db.replace_video_result(*self.key, 10, finish=True)
        self.assertEqual(self.force(15), ("accepted", 10, self.now))
        self.assertFalse(self.db.replace_video_result(*self.key, 70, finish=True))
        self.db.set_video_job_state(*self.key, "analyzing")
        self.db.finish_video_job(*self.key, "failed")
        job = self.db.get_video_job(*self.key)
        self.assertEqual(job.state, "done")
        self.assertTrue(job.actions_closed)
        self.assertEqual(self.db.get_video_result(*self.key), (15, False))
        self.assertEqual(self.force(20)[1], 15)
        self.assertEqual(personal_totals(self.db, -100, 7, self.now.date(), ZoneInfo("UTC")), (20, 20))

    def test_first_result_and_pending_removed(self):
        create_pending_manual(self.db, *self.key, 7, "A", self.now)
        self.assertEqual(self.force(15), ("accepted", None, self.now))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM pending_manual").fetchone()[0], 0)
        self.assertEqual(self.db.conn.execute("SELECT was_manual FROM video_counts").fetchone()[0], 0)

    def test_preserves_manual_quota(self):
        self.db.replace_video_result(*self.key, 10, is_manual=True)
        self.force(15)
        self.assertEqual(self.db.conn.execute("SELECT was_manual FROM video_counts").fetchone()[0], 1)

    def test_duplicate_and_unknown_and_wrong_author(self):
        tz = ZoneInfo("UTC")
        self.db.check_video_fingerprints(*self.key, {"file_id":"file"}, tz)
        self.db.create_video_job(-100, 43, 7, "A", "file", 20, 100, self.now)
        self.db.check_video_fingerprints(-100, 43, {"file_id":"file"}, tz)
        self.assertEqual(self.db.force_video_result(-100, 43, 7, 15, 99)[0], "duplicate")
        self.assertEqual(self.db.force_video_result(-100, 44, 7, 15, 99)[0], "not_found")
        self.assertEqual(self.db.force_video_result(*self.key, 8, 15, 99)[0], "wrong_author")

    def test_legacy_result_and_daily_override(self):
        self.db.save_video(-100, 12, 7, "A", 10, self.now)
        self.assertEqual(self.db.force_video_result(-100, 12, 7, 15, 99)[0], "accepted")
        self.db.set_daily_override(-100, 7, "A", self.now.date(), 40, 99)
        self.assertEqual(self.db.daily_user_total(-100, 7, self.now.date(), ZoneInfo("UTC")), 40)
