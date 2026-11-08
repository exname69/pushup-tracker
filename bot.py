from __future__ import annotations

import asyncio
import html
import logging
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import ForceReply, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from storage import Storage
from vision import analyze_video

log = logging.getLogger(__name__)
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_MANUAL_COUNT = 1000


def setup_manual_storage(db: Storage) -> None:
    """Migrate the original SQLite schema without changing storage.py."""
    columns = {row[1] for row in db.conn.execute("PRAGMA table_info(video_counts)")}
    with db.conn:
        if "is_manual" not in columns:
            db.conn.execute(
                "ALTER TABLE video_counts ADD COLUMN is_manual INTEGER NOT NULL DEFAULT 0"
            )
        db.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_manual (
                chat_id INTEGER NOT NULL,
                video_message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                sent_at_utc TEXT NOT NULL,
                prompt_message_id INTEGER,
                PRIMARY KEY (chat_id, video_message_id)
            )
            """
        )


def daily_report_rows(
    db: Storage, chat_id: int, day: date, tz: ZoneInfo, daily_target: int = 100
) -> list[tuple[str, int, int, int, int]]:
    """Return (name, reps, videos, manual_videos, goal) for each known member.

    Any missed *effective* daily goal makes the next day's goal 3x the base.
    The multiplier does not compound: 100 -> 300 -> 300 until the goal is met.
    No missed days are assigned before a member's first recorded video.
    """
    end_utc = datetime.combine(
        day + timedelta(days=1), datetime.min.time(), tzinfo=tz
    ).astimezone(timezone.utc).isoformat()
    video_rows = db.conn.execute(
        """
        SELECT m.user_id, m.display_name, v.count, v.is_manual, v.sent_at_utc
        FROM video_counts v
        JOIN members m ON m.chat_id = v.chat_id AND m.user_id = v.user_id
        WHERE v.chat_id = ? AND v.sent_at_utc < ?
        ORDER BY m.display_name COLLATE NOCASE, m.user_id, v.sent_at_utc
        """,
        (chat_id, end_utc),
    ).fetchall()

    members: dict[int, dict] = {}
    for user_id, name, count, manual, timestamp in video_rows:
        video_day = datetime.fromisoformat(timestamp).astimezone(tz).date()
        member = members.setdefault(user_id, {"name": name, "first": video_day, "days": {}})
        totals = member["days"].setdefault(video_day, [0, 0, 0])
        totals[0] += count
        totals[1] += 1
        totals[2] += manual

    result = []
    for member in sorted(members.values(), key=lambda m: m["name"].casefold()):
        current_day = member["first"]
        goal = daily_target
        while current_day < day:
            done = member["days"].get(current_day, [0])[0]
            goal = daily_target if done >= goal else daily_target * 2 + 10
            current_day += timedelta(days=1)
        reps, videos, manual = member["days"].get(day, [0, 0, 0])
        result.append((member["name"], reps, videos, manual, goal))
    return result


def report_text(day: date, rows: list[tuple[str, int, int, int, int]], target: int = 100) -> str:
    lines = [
        f"🏋️ <b>Отжимания за {day:%d.%m.%Y}</b>",
        f"🎯 Обычная норма: <b>{target}</b> отжиманий",
        "",
    ]
    for name, count, videos, manual, goal in rows:
        status = "✅" if count >= goal else "❌"
        suffix = f" · ✍️ {manual} вручную" if manual else ""
        lines.append(f"{status} <b>{html.escape(name)}</b> — {count}/{goal} · 🎥 {videos}{suffix}")
        if goal > target:
            lines.append(f"   🔥 <b>СЕГОДНЯ ШТРАФНАЯ НОРМА: {goal}!</b>")
        if count < goal:
            lines.append(f"   🚨 <b>НА {day + timedelta(days=1):%d.%m} НАЗНАЧЕНА НОРМА ×2 + 10: {target * 3}!</b>")
    if not rows:
        lines.append("Пока нет зарегистрированных участников с кружочками.")
    if any(manual for _, _, _, manual, _ in rows):
        lines += ["", "✍️ Результаты введены вручную — их можно перепроверить по видео."]
    return "\n".join(lines)


async def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Укажите BOT_TOKEN в .env")
    tz = ZoneInfo(os.getenv("REPORT_TIMEZONE", "Europe/Zurich"))
    try:
        daily_target = int(os.getenv("DAILY_TARGET", "100"))
    except ValueError as exc:
        raise ValueError("DAILY_TARGET должен быть целым положительным числом") from exc
    if daily_target <= 0:
        raise ValueError("DAILY_TARGET должен быть больше нуля")
    db = Storage(os.getenv("DB_PATH", "./data/pushups.sqlite3"))
    setup_manual_storage(db)
    dp = Dispatcher()
    semaphore = asyncio.Semaphore(2)
    report_lock = asyncio.Lock()

    def get_report(chat_id: int, day: date) -> str:
        return report_text(day, daily_report_rows(db, chat_id, day, tz, daily_target), daily_target)

    async with Bot(token=token) as bot:
        async def ask_manual(message: Message, reason: str) -> None:
            """Persist the request before sending a ForceReply prompt."""
            if not message.from_user or not message.video_note:
                return
            chat_id, message_id = message.chat.id, message.message_id
            if db.has_video(chat_id, message_id):
                return
            existing = db.conn.execute(
                "SELECT 1 FROM pending_manual WHERE chat_id=? AND video_message_id=?",
                (chat_id, message_id),
            ).fetchone()
            if existing:
                return
            with db.conn:
                db.conn.execute(
                    """
                    INSERT OR IGNORE INTO pending_manual
                        (chat_id, video_message_id, user_id, display_name, sent_at_utc)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id, message_id, message.from_user.id,
                        message.from_user.full_name,
                        message.date.astimezone(timezone.utc).isoformat(),
                    ),
                )
            try:
                prompt = await message.reply(
                    f"⚠️ {reason}\n"
                    f"✍️ {html.escape(message.from_user.full_name)}, укажи количество отжиманий "
                    f"числом от 0 до {MAX_MANUAL_COUNT} в ответ на это сообщение.\n"
                    "Результат будет помечен как введённый вручную — его можно перепроверить по кружочку.",
                    parse_mode="HTML",
                    reply_markup=ForceReply(input_field_placeholder="Например: 25", selective=True),
                )
            except Exception:
                log.exception("Не удалось запросить ручной ввод chat=%s msg=%s", chat_id, message_id)
                return
            with db.conn:
                db.conn.execute(
                    """
                    UPDATE pending_manual SET prompt_message_id=?
                    WHERE chat_id=? AND video_message_id=?
                    """,
                    (prompt.message_id, chat_id, message_id),
                )

        async def publish_yesterday() -> None:
            async with report_lock:
                yesterday = datetime.now(tz).date() - timedelta(days=1)
                for chat_id in db.chat_ids():
                    first_seen = db.chat_first_seen(chat_id)
                    if first_seen is None or first_seen.astimezone(tz).date() > yesterday:
                        continue
                    if db.report_sent(chat_id, yesterday):
                        continue
                    try:
                        await bot.send_message(chat_id, get_report(chat_id, yesterday), parse_mode="HTML")
                    except Exception:
                        log.exception("Не удалось отправить отчёт в чат %s", chat_id)
                    else:
                        db.mark_report_sent(chat_id, yesterday)

        @dp.message(Command("start", "help"))
        async def on_start(message: Message) -> None:
            if message.chat.type in ("group", "supergroup"):
                db.register_chat(message.chat.id, message.chat.title or "Группа")
            await message.answer(
                "🏋️ Пришлите видеокружочек с отжиманиями. Я посчитаю повторы и отмечу выполнение дневной нормы.\n"
                "Если видео не распознаётся, попрошу автора написать число вручную. "
                "Такие результаты помечаются ✍️ и их можно проверить.\n"
                "Команды: /stats — сегодня, /yesterday — вчера. "
                f"Дневная норма: {daily_target}. Отчёт — в 00:00 ({tz.key})."
            )

        @dp.message(Command("stats", "yesterday"))
        async def on_stats(message: Message) -> None:
            day = datetime.now(tz).date()
            if message.text and message.text.split()[0].split("@")[0] == "/yesterday":
                day -= timedelta(days=1)
            await message.answer(get_report(message.chat.id, day), parse_mode="HTML")

        @dp.message(F.text, F.reply_to_message)
        async def on_manual_reply(message: Message) -> None:
            if not message.from_user or message.from_user.is_bot or not message.reply_to_message:
                return
            replied_id = message.reply_to_message.message_id
            pending = db.conn.execute(
                """
                SELECT video_message_id, user_id, display_name, sent_at_utc
                FROM pending_manual
                WHERE chat_id=? AND (prompt_message_id=? OR video_message_id=?)
                """,
                (message.chat.id, replied_id, replied_id),
            ).fetchone()
            if pending is None:
                return
            original_id, user_id, name, sent_at_utc = pending
            if message.from_user.id != user_id:
                await message.reply("🔒 Количество может указать только автор кружочка.")
                return
            value = (message.text or "").strip()
            if not value.isascii() or not value.isdecimal() or len(value) > 4 or int(value) > MAX_MANUAL_COUNT:
                await message.reply(f"Напиши только число от 0 до {MAX_MANUAL_COUNT} в ответ на запрос бота.")
                return
            count = int(value)
            # No await between saving and deleting the pending row: avoid duplicate counts.
            inserted = db.save_video(
                message.chat.id, original_id, user_id, name,
                count, datetime.fromisoformat(sent_at_utc),
            )
            with db.conn:
                if inserted:
                    db.conn.execute(
                        """
                        UPDATE video_counts SET is_manual=1
                        WHERE chat_id=? AND message_id=?
                        """,
                        (message.chat.id, original_id),
                    )
                db.conn.execute(
                    "DELETE FROM pending_manual WHERE chat_id=? AND video_message_id=?",
                    (message.chat.id, original_id),
                )
            if inserted:
                await message.reply(
                    f"✍️ <b>{html.escape(name)}</b>: <b>{count}</b> отжиманий — "
                    "указано вручную, можно перепроверить по видео.",
                    parse_mode="HTML",
                )

        @dp.message(F.video_note)
        async def on_video_note(message: Message) -> None:
            if message.chat.type not in ("group", "supergroup"):
                await message.answer("Добавьте меня в группу: там я считаю отжимания и публикую выполнение дневной нормы.")
                return
            if not message.from_user or message.from_user.is_bot:
                return
            note = message.video_note
            db.register_chat(message.chat.id, message.chat.title or "Группа")
            if db.has_video(message.chat.id, message.message_id):
                return
            if note.file_size and note.file_size > MAX_DOWNLOAD_BYTES:
                await ask_manual(message, "Кружочек превышает лимит скачивания (20 МБ).")
                return

            try:
                async with semaphore:
                    with tempfile.TemporaryDirectory(prefix="pushup-") as tempdir:
                        target = Path(tempdir) / "note.mp4"
                        await bot.download(note.file_id, destination=target, timeout=90)
                        analysis = await asyncio.to_thread(analyze_video, target)
            except Exception:
                log.exception("Ошибка анализа кружочка chat=%s msg=%s", message.chat.id, message.message_id)
                await ask_manual(message, "Не удалось обработать этот кружочек автоматически.")
                return

            if analysis is None:
                await ask_manual(message, "Не удалось уверенно распознать отжимания.")
                return

            name = message.from_user.full_name
            inserted = db.save_video(
                message.chat.id, message.message_id, message.from_user.id,
                name, analysis.repetitions, message.date,
            )
            if inserted:
                await message.reply(
                    f"✅ {html.escape(name)}: <b>{analysis.repetitions}</b> отжиманий",
                    parse_mode="HTML",
                )

        scheduler = AsyncIOScheduler(timezone=tz)
        scheduler.add_job(
            publish_yesterday, "cron", hour=0, minute=0,
            id="daily_report", max_instances=1, coalesce=True,
        )
        scheduler.start()
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            await publish_yesterday()
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            scheduler.shutdown(wait=False)
            db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
