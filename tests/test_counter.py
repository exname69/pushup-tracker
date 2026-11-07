import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from storage import Storage
from vision import PushupCounter, joint_angle


class CounterTests(unittest.TestCase):
    def test_one_pushup(self):
        c = PushupCounter()
        for angle in [170] * 4 + [90] * 4 + [168] * 4:
            c.update(angle)
        self.assertEqual(c.repetitions, 1)

    def test_two_pushups(self):
        c = PushupCounter()
        for angle in [168] * 3 + ([95] * 3 + [165] * 3) * 2:
            c.update(angle)
        self.assertEqual(c.repetitions, 2)

    def test_no_incomplete_rep(self):
        c = PushupCounter()
        for angle in [170] * 3 + [90] * 3:
            c.update(angle)
        self.assertEqual(c.repetitions, 0)

    def test_missing_pose_breaks_cycle(self):
        c = PushupCounter()
        for angle in [170] * 3 + [90] * 3 + [None] * 9 + [170] * 3:
            c.update(angle)
        self.assertEqual(c.repetitions, 0)

    def test_jitter_near_thresholds(self):
        c = PushupCounter()
        for angle in [160, 153, 160, 160, 130, 110, 104, 112, 103, 108, 162]:
            c.update(angle)
        self.assertEqual(c.repetitions, 0)

    def test_angle(self):
        self.assertAlmostEqual(joint_angle((1, 0), (0, 0), (0, 1)), 90)
        self.assertAlmostEqual(joint_angle((-1, 0), (0, 0), (1, 0)), 180)


class StorageTests(unittest.TestCase):
    def test_two_members_and_groups(self):
        with tempfile.TemporaryDirectory() as d:
            storage = Storage(str(Path(d) / "stats.db"))
            try:
                tz = ZoneInfo("Europe/Zurich")
                instant = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
                storage.register_chat(-100, "Одна группа")
                storage.register_chat(-200, "Другая группа")
                storage.save_video(-100, 1, 10, "Анна", 10, instant)
                storage.save_video(-100, 2, 10, "Анна", 12, instant)
                storage.save_video(-100, 3, 20, "Олег", 8, instant)
                storage.save_video(-200, 1, 20, "Олег", 30, instant)
                self.assertEqual(storage.daily_results(-100, date(2026, 11, 7), tz),
                                 [("Анна", 22, 2), ("Олег", 8, 1)])
                self.assertEqual(storage.daily_results(-200, date(2026, 11, 7), tz),
                                 [("Олег", 30, 1)])
            finally:
                storage.close()

    def test_idempotency_and_midnight(self):
        with tempfile.TemporaryDirectory() as d:
            storage = Storage(str(Path(d) / "stats.db"))
            try:
                storage.register_chat(-100, "Соревнование")
                tz = ZoneInfo("Europe/Zurich")
                # 23:30 UTC 7 ноября = 00:30 8 ноября в Цюрихе.
                instant = datetime(2026, 11, 7, 23, 30, tzinfo=timezone.utc)
                self.assertTrue(storage.save_video(-100, 42, 1, "Иван", 23, instant))
                self.assertFalse(storage.save_video(-100, 42, 1, "Иван", 23, instant))
                self.assertEqual(storage.daily_results(-100, date(2026, 11, 7), tz), [])
                self.assertEqual(storage.daily_results(-100, date(2026, 11, 8), tz), [("Иван", 23, 1)])
                storage.mark_report_sent(-100, date(2026, 11, 8))
                self.assertTrue(storage.report_sent(-100, date(2026, 11, 8)))
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
