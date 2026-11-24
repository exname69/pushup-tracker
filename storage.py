"""SQLite: группы, участники, результаты видео, корректировки и отчёты."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

VIDEO_ACTION_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class VideoJob:
    chat_id: int
    message_id: int
    user_id: int
    display_name: str
    file_id: str
    duration: int
    file_size: int | None
    sent_at_utc: str
    widget_message_id: int | None
    state: str
    action_expires_at_utc: str | None
    error: str
    message_thread_id: int | None = None
    actions_closed: bool = False
    attempt_completed: bool = False


_VIDEO_JOB_COLUMNS = (
    "chat_id, message_id, user_id, display_name, file_id, duration, file_size, "
    "sent_at_utc, widget_message_id, state, action_expires_at_utc, error, "
    "message_thread_id, actions_closed, attempt_completed"
)


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
            CREATE TABLE IF NOT EXISTS video_jobs (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                file_id TEXT NOT NULL,
                duration INTEGER NOT NULL,
                file_size INTEGER,
                sent_at_utc TEXT NOT NULL,
                widget_message_id INTEGER,
                state TEXT NOT NULL DEFAULT 'queued',
                action_expires_at_utc TEXT,
                error TEXT NOT NULL DEFAULT '',
                message_thread_id INTEGER,
                actions_closed INTEGER NOT NULL DEFAULT 0,
                widget_dirty INTEGER NOT NULL DEFAULT 1,
                attempt_completed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS ix_video_jobs_expiry
                ON video_jobs(actions_closed, action_expires_at_utc);
            CREATE TABLE IF NOT EXISTS reports (
                chat_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                sent_at_utc TEXT NOT NULL,
                PRIMARY KEY (chat_id, day)
            );
            CREATE TABLE IF NOT EXISTS daily_overrides (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                count INTEGER NOT NULL CHECK (count >= 0),
                updated_at_utc TEXT NOT NULL,
                updated_by INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id, day)
            );
            CREATE INDEX IF NOT EXISTS ix_daily_overrides_chat_day
                ON daily_overrides(chat_id, day);
            """
        )
        with self.conn:
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(video_jobs)")}
            if "widget_dirty" not in columns:
                self.conn.execute(
                    "ALTER TABLE video_jobs ADD COLUMN widget_dirty INTEGER NOT NULL DEFAULT 1"
                )
            if "attempt_completed" not in columns:
                self.conn.execute(
                    "ALTER TABLE video_jobs ADD COLUMN attempt_completed INTEGER NOT NULL DEFAULT 0"
                )
                # Preserve the action window of existing completed videos.
                self.conn.execute(
                    "UPDATE video_jobs SET attempt_completed=1 WHERE action_expires_at_utc IS NOT NULL"
                )

    def register_chat(self, chat_id: int, title: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO chats(chat_id, title, first_seen_utc) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title",
                (chat_id, title, now),
            )

    def member_name(self, chat_id: int, user_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT display_name FROM members WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        return row[0] if row else None

    def set_daily_override(
        self, chat_id: int, user_id: int, display_name: str,
        day: date, count: int, updated_by: int,
    ) -> None:
        """Установить итоговое число отжиманий пользователя за день (не прибавить)."""
        if not 0 <= count <= 100_000:
            raise ValueError("Количество должно быть от 0 до 100000")
        with self.conn:
            self.conn.execute(
                "INSERT INTO members(chat_id, user_id, display_name) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id, user_id) DO UPDATE SET display_name = excluded.display_name",
                (chat_id, user_id, display_name),
            )
            self.conn.execute(
                """INSERT INTO daily_overrides(chat_id, user_id, day, count, updated_at_utc, updated_by)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id, user_id, day)
                   DO UPDATE SET count=excluded.count,
                                 updated_at_utc=excluded.updated_at_utc,
                                 updated_by=excluded.updated_by""",
                (chat_id, user_id, day.isoformat(), count,
                 datetime.now(timezone.utc).isoformat(), updated_by),
            )

    def clear_daily_override(self, chat_id: int, user_id: int, day: date) -> bool:
        """Снять корректировку: снова использовать сумму результатов видео."""
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM daily_overrides WHERE chat_id=? AND user_id=? AND day=?",
                (chat_id, user_id, day.isoformat()),
            )
        return cursor.rowcount > 0

    def daily_overrides_until(self, chat_id: int, day: date) -> list[tuple[int, str, date, int]]:
        return [
            (user_id, name, date.fromisoformat(date_iso), count)
            for user_id, name, date_iso, count in self.conn.execute(
                """SELECT o.user_id, m.display_name, o.day, o.count
                   FROM daily_overrides o
                   JOIN members m ON m.chat_id=o.chat_id AND m.user_id=o.user_id
                   WHERE o.chat_id=? AND o.day<=?""",
                (chat_id, day.isoformat()),
            )
        ]

    def has_video(self, chat_id: int, message_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM video_counts WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ).fetchone() is not None

    def create_video_job(
        self, chat_id: int, message_id: int, user_id: int, display_name: str,
        file_id: str, duration: int, file_size: int | None, sent_at: datetime,
        message_thread_id: int | None = None,
    ) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                """INSERT OR IGNORE INTO video_jobs
                   (chat_id, message_id, user_id, display_name, file_id,
                    duration, file_size, sent_at_utc, message_thread_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, message_id, user_id, display_name, file_id, duration,
                 file_size, sent_at.astimezone(timezone.utc).isoformat(), message_thread_id),
            )
        return cursor.rowcount == 1

    def get_video_job(self, chat_id: int, message_id: int) -> VideoJob | None:
        row = self.conn.execute(
            f"SELECT {_VIDEO_JOB_COLUMNS} FROM video_jobs WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ).fetchone()
        return VideoJob(*row[:-2], bool(row[-2]), bool(row[-1])) if row else None

    def bind_video_widget(self, chat_id: int, message_id: int, widget_message_id: int | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE video_jobs SET widget_message_id=? WHERE chat_id=? AND message_id=?",
                (widget_message_id, chat_id, message_id),
            )

    def set_video_job_state(self, chat_id: int, message_id: int, state: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE video_jobs SET state=?, error='', widget_dirty=1 "
                "WHERE chat_id=? AND message_id=?",
                (state, chat_id, message_id),
            )

    def finish_video_job(
        self, chat_id: int, message_id: int, state: str, error: str = '',
        now: datetime | None = None, *, attempt_completed: bool = True,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now должен содержать timezone")
        deadline = (now.astimezone(timezone.utc) + VIDEO_ACTION_TTL).isoformat()
        with self.conn:
            self.conn.execute(
                """UPDATE video_jobs SET state=?, error=?, widget_dirty=1,
                       action_expires_at_utc=CASE
                           WHEN attempt_completed=0 AND ? THEN ?
                           ELSE COALESCE(action_expires_at_utc, ?) END,
                       actions_closed=CASE WHEN attempt_completed=0 AND ? THEN 0 ELSE actions_closed END,
                       attempt_completed=MAX(attempt_completed, ?)
                   WHERE chat_id=? AND message_id=?""",
                (state, error, attempt_completed, deadline, deadline,
                 attempt_completed, attempt_completed, chat_id, message_id),
            )

    def video_jobs_to_recover(self) -> list[VideoJob]:
        return [
            VideoJob(*row[:-2], bool(row[-2]), bool(row[-1]))
            for row in self.conn.execute(
                f"SELECT {_VIDEO_JOB_COLUMNS} FROM video_jobs "
                "WHERE state IN ('queued', 'downloading', 'analyzing')"
            )
        ]

    def video_widgets_to_refresh(self) -> list[VideoJob]:
        return [
            VideoJob(*row[:-2], bool(row[-2]), bool(row[-1]))
            for row in self.conn.execute(
                f"SELECT {_VIDEO_JOB_COLUMNS} FROM video_jobs WHERE widget_dirty=1"
            )
        ]

    def mark_video_widget_clean(self, chat_id: int, message_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE video_jobs SET widget_dirty=0 WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            )

    def expired_video_widgets(self, now: datetime | None = None) -> list[VideoJob]:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now должен содержать timezone")
        return [
            VideoJob(*row[:-2], bool(row[-2]), bool(row[-1]))
            for row in self.conn.execute(
                f"SELECT {_VIDEO_JOB_COLUMNS} FROM video_jobs "
                "WHERE actions_closed=0 AND widget_message_id IS NOT NULL "
                "AND action_expires_at_utc<=?",
                (now.astimezone(timezone.utc).isoformat(),),
            )
        ]

    def mark_video_actions_closed(self, chat_id: int, message_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE video_jobs SET actions_closed=1 WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            )

    def get_video_result(self, chat_id: int, message_id: int) -> tuple[int, bool] | None:
        row = self.conn.execute(
            "SELECT count, is_manual FROM video_counts WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ).fetchone()
        return (row[0], bool(row[1])) if row else None

    def replace_video_result(
        self, chat_id: int, message_id: int, count: int, is_manual: bool = False,
    ) -> bool:
        """Заменить результат кружочка, сохранив автора, дату и расход ручной квоты."""
        if type(count) is not int or count < 0:
            raise ValueError("Количество должно быть целым неотрицательным числом")
        job = self.get_video_job(chat_id, message_id)
        if job is None:
            return False
        with self.conn:
            self.conn.execute(
                "INSERT INTO members(chat_id, user_id, display_name) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id, user_id) DO UPDATE SET display_name = excluded.display_name",
                (chat_id, job.user_id, job.display_name),
            )
            self.conn.execute(
                """INSERT INTO video_counts
                   (chat_id, message_id, user_id, count, sent_at_utc, is_manual, was_manual)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id, message_id) DO UPDATE SET
                       count=excluded.count, is_manual=excluded.is_manual,
                       was_manual=MAX(video_counts.was_manual, excluded.was_manual)""",
                (chat_id, message_id, job.user_id, count, job.sent_at_utc,
                 int(is_manual), int(is_manual)),
            )
        return True

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

    def daily_user_total(
        self, chat_id: int, user_id: int, day: date, tz: ZoneInfo
    ) -> int | None:
        """Итог дня конкретного пользователя с учётом корректировки, если есть."""
        row = self.conn.execute(
            "SELECT count FROM daily_overrides WHERE chat_id=? AND user_id=? AND day=?",
            (chat_id, user_id, day.isoformat()),
        ).fetchone()
        if row:
            return row[0]
        start = datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc).isoformat()
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc).isoformat()
        row = self.conn.execute(
            """SELECT SUM(count) FROM video_counts
               WHERE chat_id=? AND user_id=? AND sent_at_utc>=? AND sent_at_utc<?""",
            (chat_id, user_id, start, end),
        ).fetchone()
        return row[0] if row else None

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
