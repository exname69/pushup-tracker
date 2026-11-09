"""Логика отчётов и безопасного ручного ввода без зависимости от Telegram API."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

MAX_MANUAL_COUNT = 1000
MANUAL_INPUT_TTL = timedelta(minutes=15)
PENALTY_EXTRA = 10


def parse_manual_count(raw: str | None) -> int | None:
    """Принимает только ASCII-цифры 0..1000 (не Python-выражения и не SQL)."""
    text = (raw or "").strip()
    if not re.fullmatch(r"[0-9]{1,4}", text):
        return None
    count = int(text)
    return count if count <= MAX_MANUAL_COUNT else None


def manual_expires_at(created: datetime) -> datetime:
    if created.tzinfo is None:
        raise ValueError("created должен содержать timezone")
    return created.astimezone(timezone.utc) + MANUAL_INPUT_TTL


def is_manual_expired(expires_at_utc: str | None, now: datetime | None = None) -> bool:
    """Старые pending-записи без срока действия считаются просроченными."""
    if not expires_at_utc:
        return True
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now должен содержать timezone")
    try:
        until = datetime.fromisoformat(expires_at_utc)
    except ValueError:
        return True
    return until.tzinfo is None or now.astimezone(timezone.utc) >= until.astimezone(timezone.utc)



def setup_manual_storage(db: Any) -> None:
    """Безопасная идемпотентная миграция существующей базы без потери результатов."""
    with db.conn:
        columns = {row[1] for row in db.conn.execute("PRAGMA table_info(video_counts)")}
        if "is_manual" not in columns:
            db.conn.execute(
                "ALTER TABLE video_counts ADD COLUMN is_manual INTEGER NOT NULL DEFAULT 0"
            )
        db.conn.execute(
            """CREATE TABLE IF NOT EXISTS pending_manual (
                chat_id INTEGER NOT NULL,
                video_message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                sent_at_utc TEXT NOT NULL,
                prompt_message_id INTEGER,
                expires_at_utc TEXT,
                PRIMARY KEY(chat_id, video_message_id)
            )"""
        )
        columns = {row[1] for row in db.conn.execute("PRAGMA table_info(pending_manual)")}
        if "expires_at_utc" not in columns:
            db.conn.execute("ALTER TABLE pending_manual ADD COLUMN expires_at_utc TEXT")
        db.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_pending_manual_expiry "
            "ON pending_manual(expires_at_utc)"
        )


def create_pending_manual(
    db: Any, chat_id: int, video_message_id: int, user_id: int,
    display_name: str, sent_at_utc: datetime, created_at: datetime | None = None,
) -> bool:
    now = created_at or datetime.now(timezone.utc)
    with db.conn:
        cursor = db.conn.execute(
            """INSERT OR IGNORE INTO pending_manual
               (chat_id, video_message_id, user_id, display_name, sent_at_utc, expires_at_utc)
               VALUES(?,?,?,?,?,?)""",
            (chat_id, video_message_id, user_id, display_name,
             sent_at_utc.astimezone(timezone.utc).isoformat(), manual_expires_at(now).isoformat()),
        )
    return cursor.rowcount == 1


@dataclass(frozen=True)
class ManualReplyResult:
    status: str  # not_found, forbidden, expired, invalid, duplicate, accepted
    count: int | None = None
    name: str | None = None


def process_manual_reply(
    db: Any, chat_id: int, replied_message_id: int, sender_id: int,
    value: str | None, now: datetime | None = None,
) -> ManualReplyResult:
    """Проверка авторства, срока, типа, диапазона; сохранение только корректного int.

    Не содержит await: одиночный asyncio-процесс обрабатывает проверку и запись
    без переключения корутин. Уникальный ключ video_counts страхует дубли.
    """
    pending = db.conn.execute(
        """SELECT video_message_id, user_id, display_name, sent_at_utc, expires_at_utc
           FROM pending_manual WHERE chat_id=?
             AND (prompt_message_id=? OR video_message_id=?)""",
        (chat_id, replied_message_id, replied_message_id),
    ).fetchone()
    if pending is None:
        return ManualReplyResult("not_found")
    original_id, author_id, name, sent_at_utc, expires_at = pending
    if sender_id != author_id:
        return ManualReplyResult("forbidden")
    if is_manual_expired(expires_at, now):
        with db.conn:
            db.conn.execute(
                "DELETE FROM pending_manual WHERE chat_id=? AND video_message_id=?",
                (chat_id, original_id),
            )
        return ManualReplyResult("expired")
    count = parse_manual_count(value)
    if count is None:
        return ManualReplyResult("invalid")
    inserted = db.save_video(
        chat_id, original_id, author_id, name, count, datetime.fromisoformat(sent_at_utc)
    )
    with db.conn:
        if inserted:
            db.conn.execute(
                "UPDATE video_counts SET is_manual=1 WHERE chat_id=? AND message_id=?",
                (chat_id, original_id),
            )
        db.conn.execute(
            "DELETE FROM pending_manual WHERE chat_id=? AND video_message_id=?",
            (chat_id, original_id),
        )
    return ManualReplyResult("accepted" if inserted else "duplicate", count, name)


def penalty_target(daily_target: int) -> int:
    return daily_target * 2 + PENALTY_EXTRA


def daily_report_rows(
    db: Any, chat_id: int, day: date, tz: ZoneInfo, daily_target: int = 100
) -> list[tuple[str, int, int, int, int, bool]]:
    """(имя, повторы, видео, ручные видео, цель, override).

    Цель на день зависит только от *завершённых* предыдущих дней начиная с
    первой записи участника. Текущий незавершённый день не порождает штраф.
    """
    end_utc = datetime.combine(
        day + timedelta(days=1), datetime.min.time(), tzinfo=tz
    ).astimezone(timezone.utc).isoformat()
    video_rows = db.conn.execute(
        """
        SELECT m.user_id, m.display_name, v.count, v.is_manual, v.sent_at_utc
        FROM video_counts v
        JOIN members m ON m.chat_id=v.chat_id AND m.user_id=v.user_id
        WHERE v.chat_id=? AND v.sent_at_utc<?
        ORDER BY m.display_name COLLATE NOCASE, m.user_id, v.sent_at_utc
        """,
        (chat_id, end_utc),
    ).fetchall()
    members: dict[int, dict] = {}
    for user_id, name, count, manual, timestamp in video_rows:
        video_day = datetime.fromisoformat(timestamp).astimezone(tz).date()
        member = members.setdefault(user_id, {"name": name, "first": video_day, "days": {}})
        member["first"] = min(member["first"], video_day)
        member["name"] = name
        totals = member["days"].setdefault(video_day, [0, 0, 0, False])
        totals[0] += count
        totals[1] += 1
        totals[2] += manual
    for user_id, name, override_day, override_count in db.daily_overrides_until(chat_id, day):
        member = members.setdefault(user_id, {"name": name, "first": override_day, "days": {}})
        member["name"] = name
        member["first"] = min(member["first"], override_day)
        totals = member["days"].setdefault(override_day, [0, 0, 0, False])
        totals[0] = override_count
        totals[3] = True
    result = []
    for member in sorted(members.values(), key=lambda m: m["name"].casefold()):
        current_day = member["first"]
        goal = daily_target
        while current_day < day:
            done = member["days"].get(current_day, [0])[0]
            goal = daily_target if done >= goal else penalty_target(daily_target)
            current_day += timedelta(days=1)
        reps, videos, manual, overridden = member["days"].get(day, [0, 0, 0, False])
        result.append((member["name"], reps, videos, manual, goal, overridden))
    return result


def today_text(day: date, rows: list[tuple[str, int, int, int, int, bool]]) -> str:
    """Только сегодняшние отжимания: никаких штрафов/оценок/технических меток."""
    lines = [f"🏋️ <b>Отжимания за {day:%d.%m.%Y}</b>", ""]
    if not rows:
        lines.append("Пока нет зарегистрированных участников с результатами.")
    else:
        lines.extend(f"{html.escape(name)} — <b>{count}</b>" for name, count, *_ in rows)
    return "\n".join(lines)


def report_text(
    day: date, rows: list[tuple[str, int, int, int, int, bool]], target: int = 100
) -> str:
    """Итог ЗАКРЫТОГО дня; использовать для /yesterday и cron, не для /stats."""
    lines = [
        f"🏋️ <b>Итоги за {day:%d.%m.%Y}</b>",
        f"🎯 Обычная норма: <b>{target}</b> отжиманий", "",
    ]
    for name, count, videos, manual, goal, overridden in rows:
        status = "✅" if count >= goal else "❌"
        suffix = f" · ✍️ {manual} вручную" if manual else ""
        if overridden:
            suffix += " · 🛠️ скорректировано"
        lines.append(
            f"{status} <b>{html.escape(name)}</b> — {count}/{goal} · 🎥 {videos}{suffix}"
        )
        if goal > target:
            lines.append(f"   🔥 Штрафная норма этого дня: <b>{goal}</b>")
        if count < goal:
            lines.append(
                f"   🚨 На {day + timedelta(days=1):%d.%m} назначена норма: "
                f"<b>{penalty_target(target)}</b>"
            )
    if not rows:
        lines.append("Пока нет зарегистрированных участников с результатами.")
    if any(manual for _, _, _, manual, _, _ in rows):
        lines += ["", "✍️ Ручные результаты можно перепроверить по видео."]
    if any(overridden for _, _, _, _, _, overridden in rows):
        lines += ["", "🛠️ Итоги некоторых участников изменены владельцем."]
    return "\n".join(lines)


def help_text() -> str:
    return (
        "🏋️ <b>Команды PushUp Tracker</b>\n\n"
        "/start — информация о боте\n"
        "/help — список пользовательских команд\n"
        "/stats, /today — отжимания за сегодня\n"
        "/yesterday — подробные итоги вчера\n"
        "/rules, /pravila — правила челленджа\n"
        "/myid — твой Telegram ID (в личке)\n\n"
        "Отправь видеокружочек с отжиманиями. Если распознавание не удалось, "
        "появится запрос ручного ввода, действующий 15 минут."
    )


def owner_private(chat_type: str, sender_id: int | None, owner_id: int) -> bool:
    return chat_type == "private" and sender_id == owner_id
