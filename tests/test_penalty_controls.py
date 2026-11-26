import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from storage import Storage
from tracker_core import daily_report_entries, daily_report_rows, group_debug_lines, report_text, setup_manual_storage


class PenaltyControlTests(unittest.TestCase):
    def setUp(self):
        self.db = Storage(":memory:")
        setup_manual_storage(self.db)
        self.day = date(2026, 11, 25)
        self.prev = self.day - timedelta(days=1)
        self.tz = ZoneInfo("UTC")
        self.db.record_daily_target(self.day, 40, {self.prev: 30})
        for uid, name, done in ((1, "A", 20), (2, "B", 40)):
            self.db.set_daily_override(-1, uid, name, self.prev, done, 99)
            self.db.set_daily_override(-1, uid, name, self.day, 40, 99)

    def tearDown(self):
        self.db.close()

    def rows(self, day=None, chat=-1):
        return dict(daily_report_entries(self.db, chat, day or self.day, self.tz))

    def mode(self, uid, mode, day=None, chat=-1):
        self.db.set_penalty_mode(chat, uid, day or self.day, mode, 99)

    def test_group_off_and_personal_on_and_auto(self):
        self.assertEqual(self.rows()[1][4], 70)
        self.mode(0, "off")
        self.assertEqual(self.rows()[1][4], 40)
        self.mode(2, "on")
        self.assertEqual(self.rows()[2][4], 70)
        self.mode(1, "auto")
        self.assertEqual(self.rows()[1][4], 70)
        self.mode(0, "auto")
        self.assertEqual(self.rows()[2][4], 40)

    def test_waive_duplicate_without_credit_and_restore(self):
        now = datetime.combine(self.day, datetime.min.time(), tzinfo=timezone.utc)
        for msg in (1, 2):
            self.db.create_video_job(-1, msg, 1, "A", "file", 20, 100, now)
            self.db.check_video_fingerprints(-1, msg, {"file_id": "file"}, self.tz)
        self.assertEqual(self.rows()[1][4:7], (200, True, True))
        self.mode(1, "off")
        self.assertEqual(self.rows()[1][4:7], (40, True, False))
        self.assertFalse(self.db.replace_video_result(-1, 2, 99))
        self.assertEqual(self.db.get_video_job(-1, 2).state, "duplicate")
        self.assertEqual(len(self.db.duplicate_penalty_days(-1, self.day)), 1)
        self.mode(1, "auto")
        self.assertEqual(self.rows()[1][4], 200)

    def test_day_scope_and_normal_missed_goal(self):
        self.mode(0, "off")
        self.db.set_daily_override(-1, 1, "A", self.day, 10, 99)
        self.assertEqual(self.rows(self.day + timedelta(days=1))[1][4], 90)
        self.assertEqual(self.db.penalty_mode(-1, 1, self.day + timedelta(days=1)), "auto")
        self.assertEqual(self.db.penalty_mode(-2, 1, self.day), "auto")

    def test_correct_history_and_keep_current_base(self):
        self.db.set_historical_target(self.prev, 20, self.day, 40)
        self.assertEqual(self.db.daily_targets(40).on(self.prev), 20)
        self.assertEqual(self.db.daily_targets(40).on(self.day), 40)
        self.assertEqual(self.rows()[1][4], 40)
        self.db.record_daily_target(self.day, 40, {self.prev: 30})
        self.assertEqual(self.db.daily_targets(40).on(self.prev), 20)

    def test_debug_and_report_respect_overrides(self):
        self.mode(0, "off")
        self.mode(2, "on")
        debug = "\n".join(group_debug_lines(self.db, -1, self.day, self.tz, 40, 500))
        self.assertIn("B — <b>40/70</b>", debug)
        self.assertIn("A — <b>40/40</b>", debug)
        self.mode(0, "off", self.day + timedelta(days=1))
        entries = daily_report_rows(self.db, -1, self.day, self.tz)
        report = report_text(self.day, entries, 40, next_goals=[(40, False), (40, False)], next_base=40)
        self.assertIn("обычная норма: <b>40</b>", report)
        self.assertNotIn("назначена норма: <b>90</b>", report)


if __name__ == "__main__":
    unittest.main()
