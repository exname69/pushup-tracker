"""Telegram bot: видео -> анализ -> SQLite -> отчёт в 00:00."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from storage import Storage
from vision import analyze_video

log = logging.getLogger(__name__)
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


def report_text(day: date, rows: list[tuple[str, int, int]]) -> str:
    lines = [f"🏋️ <b>Отжимания за {day:%d.%m.%Y}</b>", ""]
    for i, (name, count, videos) in enumerate(rows, 1):
        lines.append(f"{i}. <b>{html.escape(name)}</b> — {count} ({videos} круж.)")
    if not rows:
        lines.append("За этот день учтённых кружочков не было.")
    lines += ["", f"<b>Всего:</b> {sum(x[1] for x in rows)} отжиманий / {sum(x[2] for x in rows)} кружочков"]
    return "\n".join(lines)


async def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Укажите BOT_TOKEN в .env")
    tz = ZoneInfo(os.getenv("REPORT_TIMEZONE", "Europe/Zurich"))
    db = Storage(os.getenv("DB_PATH", "./data/pushups.sqlite3"))
    dp = Dispatcher()
    semaphore = asyncio.Semaphore(2)
    report_lock = asyncio.Lock()

    async with Bot(token=token) as bot:
        async def publish_yesterday() -> None:
            """При старте тоже догоняем вчерашний отчёт после простоя."""
            async with report_lock:
                today = datetime.now(tz).date()
                yesterday = today - timedelta(days=1)
                for chat_id in db.chat_ids():
                    first_seen = db.chat_first_seen(chat_id)
                    if first_seen is None or first_seen.astimezone(tz).date() > yesterday:
                        continue
                    if db.report_sent(chat_id, yesterday):
                        continue
                    rows = db.daily_results(chat_id, yesterday, tz)
                    try:
                        await bot.send_message(chat_id, report_text(yesterday, rows), parse_mode="HTML")
                    except Exception:
                        log.exception("Не удалось отправить отчёт в чат %s", chat_id)
                    else:
                        db.mark_report_sent(chat_id, yesterday)

        @dp.message(Command("start", "help"))
        async def on_start(message: Message) -> None:
            if message.chat.type in ("group", "supergroup"):
                db.register_chat(message.chat.id, message.chat.title or "Группа")
            await message.answer(
                "🏋️ Пришлите видеокружочек с отжиманиями. "
                "Я посчитаю полные повторы и соберу рейтинг за день.\n"
                "Команды: /stats — сегодня, /yesterday — вчера. "
                f"Ежедневный отчёт — в 00:00 ({tz.key}).\n"
                "Для лучшего результата снимайте сбоку, чтобы тело было видно целиком."
            )

        @dp.message(Command("stats", "yesterday"))
        async def on_stats(message: Message) -> None:
            day = datetime.now(tz).date()
            if message.text and message.text.split()[0].split("@")[0] == "/yesterday":
                day -= timedelta(days=1)
            await message.answer(report_text(day, db.daily_results(message.chat.id, day, tz)), parse_mode="HTML")

        @dp.message(F.video_note)
        async def on_video_note(message: Message) -> None:
            if message.chat.type not in ("group", "supergroup"):
                await message.answer("Добавьте меня в группу: там я считаю отжимания и публикую рейтинг.")
                return
            if not message.from_user or message.from_user.is_bot:
                return

            note = message.video_note
            db.register_chat(message.chat.id, message.chat.title or "Группа")
            if db.has_video(message.chat.id, message.message_id):
                return
            if note.file_size and note.file_size > MAX_DOWNLOAD_BYTES:
                await message.reply("Не могу обработать кружочек больше 20 МБ через обычный Bot API.")
                return

            try:
                async with semaphore:
                    with tempfile.TemporaryDirectory(prefix="pushup-") as tempdir:
                        target = Path(tempdir) / "note.mp4"
                        await bot.download(note.file_id, destination=target, timeout=90)
                        analysis = await asyncio.to_thread(analyze_video, target)
            except Exception:
                log.exception("Ошибка анализа кружочка chat=%s msg=%s", message.chat.id, message.message_id)
                await message.reply("⚠️ Не удалось обработать видео. Попробуйте записать новый кружочек.")
                return

            if analysis is None:
                await message.reply("⚠️ Не удалось уверенно распознать отжимания. Снимите сбоку, чтобы было видно всё тело.")
                return

            name = message.from_user.full_name
            inserted = db.save_video(
                message.chat.id, message.message_id, message.from_user.id,
                name, analysis.repetitions, message.date,
            )
            if inserted:
                await message.reply(f"✅ {html.escape(name)}: <b>{analysis.repetitions}</b> отжиманий", parse_mode="HTML")

        scheduler = AsyncIOScheduler(timezone=tz)
        scheduler.add_job(
            publish_yesterday, "cron", hour=0, minute=0,
            id="daily_report", max_instances=1, coalesce=True,
        )
        scheduler.start()
        try:
            # Не теряем необработанные апдейты при кратком простое.
            await bot.delete_webhook(drop_pending_updates=False)
            await publish_yesterday()
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            scheduler.shutdown(wait=False)
            db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
