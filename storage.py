"""SQLite: группы, участники, результаты видео, корректировки и отчёты."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

VIDEO_ACTION_TTL = timedelta(minutes=30)
STRONG_RETRY_TTL = timedelta(minutes=5)
VIDEO_PROCESS_TIMEOUT_SECONDS = 120  # Общий предел: очередь + скачивание + Gemini


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
    result_source: str = ""  # strong / weak / manual / пусто, если результата нет
    retry_limit: int = 2
    retry_used: int = 0
    result_locked: bool = False
    actions_closed: bool = False


_VIDEO_JOB_COLUMNS = (
    "chat_id, message_id, user_id, display_name, file_id, duration, file_size, "
    "sent_at_utc, widget_message_id, state, action_expires_at_utc, error, "
    "message_thread_id, result_source, retry_limit, retry_used, result_locked, actions_closed"
)


def video_action_flags(job: VideoJob, now: datetime | None = None) -> tuple[bool, bool]:
    """(можно повторить, можно ввести вручную), независимо от наличия кнопки."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now должен содержать timezone")
    if job.state not in ("done", "failed") or job.result_locked or job.actions_closed:
        return False, False
    if not job.action_expires_at_utc:
        return False, False
    deadline = datetime.fromisoformat(job.action_expires_at_utc)
    if deadline.tzinfo is None or now >= deadline:
        return False, False
    if job.retry_used < job.retry_limit:
        return True, False
    # После первого успешного подсчёта сильной моделью ручная правка не нужна.
    if job.retry_limit == 2 and job.retry_used >= 2 and job.result_source != "strong":
        return False, True
    return False, False


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
                result_source TEXT NOT NULL DEFAULT '',
                retry_limit INTEGER NOT NULL DEFAULT 2,
                retry_used INTEGER NOT NULL DEFAULT 0,
                result_locked INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS ix_video_jobs_expiry
                ON video_jobs(actions_closed, action_expires_at_utc);
            CREATE TABLE IF NOT EXISTS model_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT NOT NULL,
                requested_at_utc TEXT NOT NULL,
                outcome TEXT NOT NULL,
                status_code INTEGER,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_model_requests_window
                ON model_requests(model, requested_at_utc);
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
            # Старые завершённые задачи закрываем: их историю попыток восстановить нельзя.
            if "result_source" not in columns:
                self.conn.execute(
                    "ALTER TABLE video_jobs ADD COLUMN result_source TEXT NOT NULL DEFAULT ''"
                )
            if "retry_limit" not in columns:
                self.conn.execute(
                    "ALTER TABLE video_jobs ADD COLUMN retry_limit INTEGER NOT NULL DEFAULT 2"
                )
            if "retry_used" not in columns:
                self.conn.execute(
                    "ALTER TABLE video_jobs ADD COLUMN retry_used INTEGER NOT NULL DEFAULT 0"
                )
            if "result_locked" not in columns:
                self.conn.execute(
                    "ALTER TABLE video_jobs ADD COLUMN result_locked INTEGER NOT NULL DEFAULT 0"
                )
                self.conn.execute(
                    "UPDATE video_jobs SET result_locked=1, actions_closed=1, widget_dirty=1 "
                    "WHERE state IN ('done', 'failed')"
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
        return VideoJob(*row[:-1], bool(row[-1])) if row else None

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
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now должен содержать timezone")
        deadline = (now.astimezone(timezone.utc) + VIDEO_ACTION_TTL).isoformat()
        with self.conn:
            self.conn.execute(
                """UPDATE video_jobs SET state=?, error=?, widget_dirty=1,
                       action_expires_at_utc=COALESCE(action_expires_at_utc, ?)
                   WHERE chat_id=? AND message_id=?""",
                (state, error, deadline, chat_id, message_id),
            )

    def reserve_video_retry(
        self, chat_id: int, message_id: int, now: datetime | None = None,
    ) -> bool:
        """Атомарно потратить одну попытку; повторный callback не пройдёт."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now должен содержать timezone")
        with self.conn:
            cursor = self.conn.execute(
                """UPDATE video_jobs
                   SET retry_used=retry_used+1, state='queued', error='', widget_dirty=1
                   WHERE chat_id=? AND message_id=?
                     AND state IN ('done', 'failed')
                     AND result_locked=0 AND actions_closed=0
                     AND retry_used < retry_limit
                     AND action_expires_at_utc>?""",
                (chat_id, message_id, now.astimezone(timezone.utc).isoformat()),
            )
        return cursor.rowcount == 1

    def complete_video_analysis(
        self, chat_id: int, message_id: int, count: int | None,
        source: str | None = None, error: str = "", now: datetime | None = None,
    ) -> None:
        """Фиксирует результат попытки; не теряет старый подсчёт при неудаче."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now должен содержать timezone")
        if source not in (None, "strong", "weak"):
            raise ValueError("Неизвестный источник результата")
        if count is None:
            source = None
        elif type(count) is not int or count < 0 or source is None:
            raise ValueError("Для результата нужны count и source")
        job = self.get_video_job(chat_id, message_id)
        if job is None or job.result_locked:
            return

        deadline = job.action_expires_at_utc
        limit = job.retry_limit
        origin = job.result_source
        locked = False

        if source == "strong":
            self.replace_video_result(chat_id, message_id, count)
            origin = "strong"
            if job.retry_used:
                # Любой успешный повтор сильной модели — окончательный.
                locked = True
            else:
                limit = 1
                deadline = (now + STRONG_RETRY_TTL).astimezone(timezone.utc).isoformat()
        elif source == "weak":
            # Резервная модель работает только при первоначальном анализе.
            self.replace_video_result(chat_id, message_id, count)
            origin = "weak"
            limit = 2
            deadline = (now + VIDEO_ACTION_TTL).astimezone(timezone.utc).isoformat()
        elif job.retry_used:
            if job.result_source == "strong" and job.retry_limit == 1:
                # Единственный повтор не удался: сохраняем первое число и закрываем.
                locked = True
            elif job.retry_used >= job.retry_limit:
                # После двух неудачных повторов даём отдельное окно ручного ввода.
                deadline = (now + VIDEO_ACTION_TTL).astimezone(timezone.utc).isoformat()
        else:
            # Первичный анализ не дал результата ни одной моделью.
            limit = 2
            deadline = (now + VIDEO_ACTION_TTL).astimezone(timezone.utc).isoformat()

        state = "done" if self.get_video_result(chat_id, message_id) is not None else "failed"
        with self.conn:
            self.conn.execute(
                """UPDATE video_jobs
                   SET state=?, error=?, result_source=?, retry_limit=?,
                       result_locked=?, actions_closed=?, action_expires_at_utc=?,
                       widget_dirty=1
                   WHERE chat_id=? AND message_id=?""",
                (state, "" if state == "done" else error, origin, limit,
                 int(locked), int(locked), deadline, chat_id, message_id),
            )

    def lock_manual_video_result(self, chat_id: int, message_id: int) -> None:
        """Ручной ввод окончателен и больше не разрешает повторов."""
        with self.conn:
            self.conn.execute(
                """UPDATE video_jobs
                   SET state='done', error='', result_source='manual',
                       result_locked=1, actions_closed=1, widget_dirty=1
                   WHERE chat_id=? AND message_id=?""",
                (chat_id, message_id),
            )

    def lock_expired_video_jobs(self, now: datetime | None = None) -> None:
        """Истечение срока фиксируется в SQLite, даже если Telegram недоступен."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now должен содержать timezone")
        with self.conn:
            self.conn.execute(
                """UPDATE video_jobs
                   SET result_locked=1, actions_closed=1, widget_dirty=1
                   WHERE state IN ('done', 'failed')
                     AND result_locked=0 AND action_expires_at_utc IS NOT NULL
                     AND action_expires_at_utc<=?""",
                (now.astimezone(timezone.utc).isoformat(),),
            )

    def refund_transient_video_retry(
        self, chat_id: int, message_id: int, now: datetime | None = None,
    ) -> None:
        """Не списывать повтор, если все вызовы моделей упали по инфраструктуре."""
        now = now or datetime.now(timezone.utc)
        job = self.get_video_job(chat_id, message_id)
        if job is None or not job.retry_used or job.state not in (
            "queued", "downloading", "analyzing"
        ):
            return
        result_exists = self.get_video_result(chat_id, message_id) is not None
        ttl = STRONG_RETRY_TTL if job.result_source == "strong" else VIDEO_ACTION_TTL
        deadline = (now + ttl).astimezone(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """UPDATE video_jobs SET retry_used=retry_used-1,
                       state=?, error=?, action_expires_at_utc=?,
                       widget_dirty=1
                   WHERE chat_id=? AND message_id=? AND retry_used>0
                     AND state IN ('queued', 'downloading', 'analyzing')""",
                ("done" if result_exists else "failed",
                 "" if result_exists else "Сервис Gemini временно недоступен.",
                 deadline, chat_id, message_id),
            )

    def record_model_request(
        self, model: str, outcome: str, status_code: int | None = None,
        usage: dict[str, int] | None = None, now: datetime | None = None,
    ) -> None:
        """Локальная статистика вызовов, не квота аккаунта Google."""
        now = now or datetime.now(timezone.utc)
        usage = usage or {}
        with self.conn:
            self.conn.execute(
                """INSERT INTO model_requests
                   (model, requested_at_utc, outcome, status_code,
                    prompt_tokens, output_tokens, total_tokens)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (model, now.astimezone(timezone.utc).isoformat(), outcome,
                 status_code, usage.get("prompt_token_count", 0),
                 usage.get("candidates_token_count", 0),
                 usage.get("total_token_count", 0)),
            )

    def model_request_stats(self, now: datetime | None = None) -> dict[str, dict]:
        now = now or datetime.now(timezone.utc)
        results = {}
        for window, start in (
            ("minute", now - timedelta(minutes=1)),
            ("day", now - timedelta(hours=24)),
        ):
            rows = self.conn.execute(
                """SELECT model, COUNT(*),
                          SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN status_code=429 THEN 1 ELSE 0 END),
                          SUM(CASE WHEN status_code IN (503,504) THEN 1 ELSE 0 END),
                          COALESCE(SUM(prompt_tokens),0),
                          COALESCE(SUM(output_tokens),0),
                          COALESCE(SUM(total_tokens),0)
                   FROM model_requests WHERE requested_at_utc>=?
                   GROUP BY model""",
                (start.astimezone(timezone.utc).isoformat(),),
            ).fetchall()
            for model, attempts, successes, limited, unavailable, inp, out, total in rows:
                results.setdefault(model, {})[window] = (
                    attempts, successes, limited, unavailable, inp, out, total
                )
        return results

    def video_jobs_to_recover(self) -> list[VideoJob]:
        return [
            VideoJob(*row[:-1], bool(row[-1]))
            for row in self.conn.execute(
                f"SELECT {_VIDEO_JOB_COLUMNS} FROM video_jobs "
                "WHERE state IN ('queued', 'downloading', 'analyzing')"
            )
        ]

    def video_widgets_to_refresh(self) -> list[VideoJob]:
        return [
            VideoJob(*row[:-1], bool(row[-1]))
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
            VideoJob(*row[:-1], bool(row[-1]))
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
