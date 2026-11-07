"""SQLite: группы, участники, результат каждого кружочка, отправленные отчёты."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


class Storage:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                first_seen_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS members (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS video_counts (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER NOT NULL CHECK (count >= 0),
                sent_at_utc TEXT NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS ix_video_counts_chat_date
                ON video_counts(chat_id, sent_at_utc);
            CREATE TABLE IF NOT EXISTS reports (
                chat_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                sent_at_utc TEXT NOT NULL,
                PRIMARY KEY (chat_id, day)
            );
            """
        )

    def register_chat(self, chat_id: int, title: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO chats(chat_id, title, first_seen_utc) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title",
                (chat_id, title, now),
            )

    def has_video(self, chat_id: int, message_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM video_counts WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ).fetchone() is not None

    def save_video(
        self, chat_id: int, message_id: int, user_id: int,
        display_name: str, count: int, sent_at: datetime,
    ) -> bool:
        timestamp = sent_at.astimezone(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO members(chat_id, user_id, display_name) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id, user_id) DO UPDATE SET display_name = excluded.display_name",
                (chat_id, user_id, display_name),
            )
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO video_counts(chat_id, message_id, user_id, count, sent_at_utc) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, message_id, user_id, count, timestamp),
            )
        return cursor.rowcount == 1

    def chat_ids(self) -> list[int]:
        return [row[0] for row in self.conn.execute("SELECT chat_id FROM chats")]

    def chat_first_seen(self, chat_id: int) -> datetime | None:
        row = self.conn.execute("SELECT first_seen_utc FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def daily_results(self, chat_id: int, day: date, tz: ZoneInfo) -> list[tuple[str, int, int]]:
        """(имя, число отжиманий, число кружочков), календарный день в tz."""
        start = datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc).isoformat()
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc).isoformat()
        return self.conn.execute(
            """
            SELECT m.display_name, SUM(v.count) AS total, COUNT(*) AS videos
            FROM video_counts v
            JOIN members m ON m.chat_id = v.chat_id AND m.user_id = v.user_id
            WHERE v.chat_id=? AND v.sent_at_utc >= ? AND v.sent_at_utc < ?
            GROUP BY v.user_id
            ORDER BY total DESC, videos DESC, m.display_name COLLATE NOCASE
            """,
            (chat_id, start, end),
        ).fetchall()

    def report_sent(self, chat_id: int, day: date) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM reports WHERE chat_id=? AND day=?", (chat_id, day.isoformat())
        ).fetchone() is not None

    def mark_report_sent(self, chat_id: int, day: date) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO reports VALUES (?, ?, ?)",
                (chat_id, day.isoformat(), datetime.now(timezone.utc).isoformat()),
            )

    def close(self) -> None:
        self.conn.close()
