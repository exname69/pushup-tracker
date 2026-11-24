"""Personal totals and exact duplicate penalties (no live Telegram/Gemini)."""
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from storage import Storage
from tracker_core import daily_report_rows, personal_totals, personal_text, report_text, setup_manual_storage


class PersonalAndDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.db = Storage(":memory:")
        setup_manual_storage(self.db)
        self.tz = ZoneInfo("Europe/Zurich")
        self.now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
        self.chat = -100

    def tearDown(self):
        self.db.close()

    def job(self, message_id, user=7, chat=None, when=None):
        chat = self.chat if chat is None else chat
        self.db.create_video_job(chat, message_id, user, str(user), f"file-{message_id}", 20, 100, when or self.now)
        return chat, message_id

    def check(self, key, **fingerprints):
        return self.db.check_video_fingerprints(*key, fingerprints, self.tz)

    def test_personal_totals_use_calendar_periods_and_overrides(self):
        for i, d, count in [(1, 1, 10), (2, 22, 20), (3, 23, 30), (4, 24, 40), (5, 25, 90)]:
            key = self.job(i, when=self.now.replace(day=d))
            self.db.replace_video_result(*key, count)
        self.db.set_daily_override(self.chat, 7, "7", date(2026, 11, 24), 50, 1)
        self.db.replace_video_result(*self.job(6, user=8), 100)
        self.assertEqual(personal_totals(self.db, self.chat, 7, self.now.date(), self.tz), (80, 110))
        self.assertEqual(personal_totals(self.db, self.chat, 99, self.now.date(), self.tz), (0, 0))
        self.assertIn("За неделю: <b>80</b>", personal_text(80, 110))

    def test_duplicate_id_is_rejected_but_original_retry_is_not(self):
        first = self.job(1)
        self.assertFalse(self.check(first, file_id="a", file_unique_id="unique"))
        self.assertFalse(self.check(first, file_unique_id="unique"))
        self.db.replace_video_result(*first, 25)
        duplicate = self.job(2)
        self.assertTrue(self.check(duplicate, file_id="b", file_unique_id="unique"))
        self.assertEqual(self.db.get_video_job(*duplicate).state, "duplicate")
        self.assertTrue(self.db.get_video_job(*duplicate).actions_closed)
        self.assertFalse(self.db.replace_video_result(*duplicate, 25))
        self.assertIsNone(self.db.get_video_result(*duplicate))
        self.assertTrue(self.check(duplicate, file_id="b"))
        self.assertEqual(personal_totals(self.db, self.chat, 7, self.now.date(), self.tz), (25, 25))

    def test_hash_alias_and_other_group(self):
        first = self.job(1)
        self.assertFalse(self.check(first, sha256="same"))
        duplicate = self.job(2, user=8)
        self.assertFalse(self.check(duplicate, file_unique_id="second"))
        self.assertTrue(self.check(duplicate, sha256="same"))
        self.assertTrue(self.check(self.job(3), file_unique_id="second"))
        self.assertFalse(self.check(self.job(4, chat=-200), sha256="same"))

    def test_cash_fine_once_per_day_without_carryover(self):
        original = self.job(1, user=8)
        self.check(original, file_id="same")
        for i in (2, 3):
            self.assertTrue(self.check(self.job(i), file_id="same"))
        day = self.now.date()
        rows = daily_report_rows(self.db, self.chat, day, self.tz, 100)
        self.assertEqual(rows, [("7", 0, 0, 0, 500, False, True)])
        report = report_text(day, rows, 100, 750)
        self.assertEqual(report.count("750 ₽"), 1)
        self.assertNotIn("назначена норма", report)
        tomorrow = daily_report_rows(self.db, self.chat, day + timedelta(days=1), self.tz, 100)
        self.assertEqual(tomorrow[0][4], 100)
        self.db.set_daily_override(self.chat, 7, "7", day, 500, 1)
        report = report_text(day, daily_report_rows(self.db, self.chat, day, self.tz), 100, 750)
        self.assertNotIn("750 ₽", report)
        self.assertIn("денежного штрафа нет", report)

    def test_penalty_applies_to_local_submission_date(self):
        self.check(self.job(1), file_id="same")
        late = datetime(2026, 11, 24, 23, 30, tzinfo=timezone.utc)
        self.assertTrue(self.check(self.job(2, when=late), file_id="same"))
        self.assertEqual(self.db.duplicate_penalty_days(self.chat, date(2026, 11, 24)), [])
        self.assertEqual(self.db.duplicate_penalty_days(self.chat, date(2026, 11, 25)), [(7, "7", date(2026, 11, 25))])

    def test_known_file_id_survives_restart_and_backfill(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "db.sqlite")
            db = Storage(path)
            db.create_video_job(self.chat, 1, 7, "7", "old-file", 20, 100, self.now)
            db.close()
            db = Storage(path)
            try:
                db.create_video_job(self.chat, 2, 7, "7", "old-file", 20, 100, self.now)
                self.assertTrue(db.check_video_fingerprints(self.chat, 2, {"file_id": "old-file"}, self.tz))
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
