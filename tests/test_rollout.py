import asyncio
import json
import os
import sqlite3
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from bot import main, parse_target_history, process_startup_updates
from scripts.migrate_legacy_db import import_database, migrate
from storage import Storage
from tracker_core import create_pending_manual, daily_report_rows, process_manual_reply, report_text, setup_manual_storage


class TargetHistoryTests(unittest.TestCase):
    def test_startup_publishes_yesterday_once_using_yesterdays_target(self):
        now = datetime(2026, 11, 25, 5, tzinfo=timezone.utc)
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.sqlite3")
            with closing(Storage(path)) as db:
                db.register_chat(-1, "Test")
                with db.conn:
                    db.conn.execute("UPDATE chats SET first_seen_utc='2026-11-23T00:00:00+00:00'")
                db.save_video(-1, 1, 7, "Test", 30, now.replace(day=23))
            async def accept_yesterdays_video(dispatcher, bot, update):
                with closing(Storage(path)) as db:
                    db.save_video(-1, update.update_id, 7, "Test", 31, now.replace(day=24))
            settings = {
                "DB_PATH": path, "DAILY_TARGET": "40", "DAILY_TARGET_HISTORY": "2026-11-24=30",
                "REPORT_TIMEZONE": "Europe/Moscow", "BOT_TOKEN": "test", "OWNER_ID": "1",
                "ALLOWED_CHAT_IDS": "-1",
            }
            with patch.dict(os.environ, settings, clear=True), patch("bot.load_dotenv"), \
                 patch("bot.datetime", wraps=datetime) as clock, patch("bot.Bot") as client, \
                 patch("bot.Dispatcher.feed_update", new=accept_yesterdays_video), \
                 patch("bot.Dispatcher.start_polling", new_callable=AsyncMock):
                clock.now.side_effect = lambda tz: now.astimezone(tz)
                telegram = client.return_value.__aenter__.return_value
                telegram.get_webhook_info.side_effect = [SimpleNamespace(pending_update_count=n) for n in (1, 0)]
                telegram.get_updates.side_effect = [[SimpleNamespace(update_id=2)], []]
                sent = telegram.send_message
                asyncio.run(main())
                asyncio.run(main())
                sent.assert_awaited_once()
                self.assertIn("Обычная норма: <b>30</b>", sent.await_args.args[1])
                self.assertIn("✅ <b>Test</b> — 31/30", sent.await_args.args[1])

    def test_backlog_larger_than_telegram_batch_hands_new_arrivals_to_polling(self):
        bot, dispatcher = AsyncMock(), AsyncMock()
        bot.get_webhook_info.return_value.pending_update_count = 102
        updates = [SimpleNamespace(update_id=n) for n in range(1, 104)]
        bot.get_updates.side_effect = [updates[:100], updates[100:102], updates[102:]]
        asyncio.run(process_startup_updates(bot, dispatcher, ["message"]))
        self.assertEqual([call.args[1].update_id for call in dispatcher.feed_update.await_args_list],
                         list(range(1, 103)))
        self.assertEqual([(c.kwargs["offset"], c.kwargs["limit"]) for c in bot.get_updates.await_args_list],
                         [(None, 100), (101, 2), (103, 1)])

    def test_reports_survive_change_and_restart(self):
        day24, day25, day26 = (date(2026, 11, n) for n in (24, 25, 26))
        tz = ZoneInfo("Europe/Moscow")
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "bot.sqlite3")
            with closing(Storage(path)) as db:
                setup_manual_storage(db)
                people = [("Fedor", 42, 53), ("Ts Pavel", 32, 54), ("Андрей", 31, 42),
                          ("Марк", 70, 50), ("Матвей Чигирин", 39, 25)]
                db.save_video(-1, 100, 1, "Fedor", 35, datetime(2026, 11, 23, 12, tzinfo=tz))
                for uid, (name, before, after) in enumerate(people, 1):
                    for day, value in ((day24, before), (day25, after)):
                        db.set_daily_override(-1, uid, name, day, value, 10)
                    if name in ("Ts Pavel", "Марк"):
                        for msg in (uid * 10, uid * 10 + 1):
                            db.create_video_job(-1, msg, uid, name, name, 5, 10,
                                                datetime(2026, 11, 24, 12, tzinfo=tz))
                            db.check_video_fingerprints(-1, msg, {"file_id": name}, tz)
                db.mark_report_sent(-1, day24)

            history = parse_target_history("2026-11-24=30,2026-11-25=40")
            for seed in (history, {}):
                with closing(Storage(path)) as db:
                    targets = db.record_daily_target(day26, 40, seed)
                    self.assertTrue(db.report_sent(-1, day24))
                    def goals(day):
                        return {r[0]: r[4] for r in daily_report_rows(db, -1, day, tz, targets)}
                    self.assertEqual(goals(day24), {"Fedor": 30, "Ts Pavel": 150, "Андрей": 30,
                                                    "Марк": 150, "Матвей Чигирин": 30})
                    self.assertEqual(set(goals(day25).values()), {40})
                    self.assertEqual(goals(day26), {name: 90 if after < 40 else 40 for name, _, after in people})
                    rows = daily_report_rows(db, -1, day24, tz)
                    report = report_text(day24, rows, targets.on(day24), 20000)
                    self.assertIn("Обычная норма: <b>30</b>", report)
                    self.assertEqual(report.count("20000 ₽"), 2)
                    self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM daily_targets").fetchone()[0], 2)

    def test_promised_penalty_is_preserved_across_target_change(self):
        tz = ZoneInfo("UTC")
        before, after = date(2026, 11, 24), date(2026, 11, 25)
        with closing(Storage(":memory:")) as db:
            setup_manual_storage(db)
            db.record_daily_target(before, 30, {})
            db.set_daily_override(-1, 1, "Test", before, 29, 10)
            targets = db.record_daily_target(after, 40, {})
            previous = report_text(before, daily_report_rows(db, -1, before, tz), targets.on(before))
            self.assertIn("На 25.11 назначена норма: <b>70</b>", previous)
            self.assertEqual(daily_report_rows(db, -1, after, tz)[0][4], 70)
            targets = db.record_daily_target(after, 100, {})
            self.assertEqual(targets.on(before), 30)
            self.assertEqual(targets.on(after), 100)
            today = report_text(after, daily_report_rows(db, -1, after, tz), targets.on(after))
            self.assertIn("Штрафная норма этого дня: <b>70</b>", today)
            self.assertIn("На 26.11 назначена норма: <b>210</b>", today)

    def test_first_day_reconfiguration_preserves_legacy_baseline(self):
        day = date(2026, 11, 25)
        with closing(Storage(":memory:")) as db:
            db.record_daily_target(day, 30, {})
            targets = db.record_daily_target(day, 40, {})
            self.assertEqual(targets.on(date(2026, 10, 1)), 30)
            self.assertEqual(targets.on(day), 40)


class MigrationTests(unittest.TestCase):
    def test_check_of_already_mounted_database_leaves_the_bot_running(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            data = root / "data"
            data.mkdir()
            with closing(Storage(str(data / "pushups.sqlite3"))) as db:
                db.save_video(-1, 1, 7, "A", 42, datetime.now(timezone.utc))
                container = {
                    "Config": {"WorkingDir": "/app", "Env": ["DB_PATH=./data/pushups.sqlite3"]},
                    "State": {"Status": "running", "Running": True},
                    "Mounts": [{"Type": "bind", "Source": str(data), "Destination": "/app/data"}],
                }
                with patch("scripts.migrate_legacy_db.subprocess.check_output",
                           side_effect=["bot-id", json.dumps([container])]), \
                     patch("scripts.migrate_legacy_db.subprocess.run") as command:
                    migrate(root)
                    command.assert_not_called()
                self.assertEqual(db.conn.execute("SELECT count FROM video_counts").fetchone(), (42,))

    def test_interrupted_legacy_manual_reply_can_be_retried(self):
        now = datetime.now(timezone.utc)
        with closing(Storage(":memory:")) as db:
            setup_manual_storage(db)
            create_pending_manual(db, -1, 1, 7, "A", now, created_at=now)
            db.conn.execute("""CREATE TRIGGER interrupt_close BEFORE DELETE ON pending_manual
                               BEGIN SELECT RAISE(ABORT, 'interrupted'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                process_manual_reply(db, -1, 1, 7, "42", now)
            self.assertIsNone(db.get_video_result(-1, 1))
            db.conn.execute("DROP TRIGGER interrupt_close")
            self.assertEqual(process_manual_reply(db, -1, 1, 7, "42", now).status, "accepted")
            self.assertEqual(db.conn.execute("SELECT count, is_manual, was_manual FROM video_counts").fetchall(),
                             [(42, 1, 1)])
            self.assertEqual(process_manual_reply(db, -1, 1, 7, "42", now).status, "not_found")

    def test_snapshot_includes_wal_and_cannot_overwrite_a_different_database(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "legacy.sqlite3"
            live = root / "pushups.sqlite3"
            with closing(Storage(str(source))) as db:
                db.conn.execute("PRAGMA wal_autocheckpoint=0")
                db.save_video(-1, 1, 7, "A", 42, datetime.now(timezone.utc))
                self.assertGreater(Path(str(source) + "-wal").stat().st_size, 0)
                for _ in range(2):
                    with TemporaryDirectory(dir=root) as stage:
                        import_database(source, live, Path(stage))
                with closing(sqlite3.connect(live)) as current, current:
                    self.assertEqual(current.execute("SELECT count FROM video_counts").fetchone(), (42,))
                    current.execute("UPDATE video_counts SET count=53")
                with TemporaryDirectory(dir=root) as stage, self.assertRaisesRegex(ValueError, "другая база"):
                    import_database(source, live, Path(stage))
                with closing(sqlite3.connect(live)) as current:
                    self.assertEqual(current.execute("SELECT count FROM video_counts").fetchone(), (53,))
                rescued, = root.glob("legacy-*.sqlite3")
                with closing(sqlite3.connect(rescued)) as copy:
                    self.assertEqual(copy.execute("SELECT count FROM video_counts").fetchone(), (42,))

    def test_failed_job_update_rolls_back_video_result(self):
        with closing(Storage(":memory:")) as db:
            setup_manual_storage(db)
            db.create_video_job(-1, 1, 1, "A", "file", 5, None, datetime.now(timezone.utc))
            db.conn.execute("""CREATE TRIGGER interrupt_finish BEFORE UPDATE OF state ON video_jobs
                               BEGIN SELECT RAISE(ABORT, 'interrupted'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                db.replace_video_result(-1, 1, 42, finish=True)
            self.assertIsNone(db.get_video_result(-1, 1))
            self.assertEqual(db.get_video_job(-1, 1).completed_attempts, 0)
            db.conn.execute("DROP TRIGGER interrupt_finish")
            db.replace_video_result(-1, 1, 42, finish=True)
            self.assertEqual(db.get_video_result(-1, 1), (42, False))
            self.assertEqual(db.get_video_job(-1, 1).state, "done")
            self.assertEqual(db.get_video_job(-1, 1).completed_attempts, 1)
