"""PushUp Tracker: Telegram-бот, отчёты, безопасный ручной ввод и диагностика."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BotCommand, ChatMemberUpdated, ForceReply, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from storage import Storage
from tracker_core import (
    MANUAL_INPUT_TTL, MAX_MANUAL_COUNT, MANUAL_REVIEW_THRESHOLD,
    MAX_INVALID_MANUAL_ATTEMPTS, MAX_MANUAL_VIDEOS_PER_DAY, create_pending_manual, daily_report_rows,
    help_text, manual_input_error_message, owner_private, penalty_target, process_manual_reply, report_text,
    setup_manual_storage, today_text,
)
from vision import analyze_video

log = logging.getLogger(__name__)
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_DAILY_OVERRIDE = 100_000
DEBUG_GEMINI_MODEL = "gemini-3.5-flash-lite"
PUBLIC_COMMANDS = [
    BotCommand(command="start", description="Начать работу"),
    BotCommand(command="help", description="Все команды"),
    BotCommand(command="stats", description="Отжимания сегодня"),
    BotCommand(command="today", description="Отжимания сегодня"),
    BotCommand(command="yesterday", description="Итоги вчера"),
    BotCommand(command="rules", description="Правила"),
    BotCommand(command="pravila", description="Правила"),
    BotCommand(command="myid", description="Мой Telegram ID (личка)"),
]


def parse_id_list(raw: str, label: str) -> set[int]:
    result = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.add(int(item))
        except ValueError as exc:
            raise ValueError(f"В {label} указан некорректный ID: {item!r}") from exc
    return result


class AccessControl:
    """Белые списки из .env с постоянными переопределениями в SQLite."""

    def __init__(self, db: Storage, owner_id: int) -> None:
        self.db = db
        self.owner_id = owner_id
        self.env_groups = parse_id_list(os.getenv("ALLOWED_CHAT_IDS", ""), "ALLOWED_CHAT_IDS")
        self.env_users = parse_id_list(os.getenv("ALLOWED_USER_IDS", ""), "ALLOWED_USER_IDS")
        with db.conn:
            db.conn.execute(
                """CREATE TABLE IF NOT EXISTS access_rules (
                    kind TEXT NOT NULL CHECK(kind IN ('group', 'user')),
                    target_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                    PRIMARY KEY(kind, target_id)
                )"""
            )

    def allowed(self, kind: str, target_id: int) -> bool:
        if kind == "user" and target_id == self.owner_id:
            return True
        row = self.db.conn.execute(
            "SELECT enabled FROM access_rules WHERE kind=? AND target_id=?",
            (kind, target_id),
        ).fetchone()
        if row is not None:
            return bool(row[0])
        return target_id in (self.env_groups if kind == "group" else self.env_users)

    def set_allowed(self, kind: str, target_id: int, enabled: bool) -> None:
        if kind == "user" and target_id == self.owner_id and not enabled:
            raise ValueError("Нельзя запретить доступ владельцу")
        with self.db.conn:
            self.db.conn.execute(
                """INSERT INTO access_rules(kind,target_id,enabled) VALUES(?,?,?)
                   ON CONFLICT(kind,target_id) DO UPDATE SET enabled=excluded.enabled""",
                (kind, target_id, int(enabled)),
            )

    def list_allowed(self, kind: str) -> list[int]:
        baseline = self.env_groups if kind == "group" else (self.env_users | {self.owner_id})
        ids = set(baseline)
        for target_id, enabled in self.db.conn.execute(
            "SELECT target_id, enabled FROM access_rules WHERE kind=?", (kind,)
        ):
            if enabled:
                ids.add(target_id)
            else:
                ids.discard(target_id)
        return sorted(ids)


class AccessMiddleware(BaseMiddleware):
    def __init__(self, access: AccessControl) -> None:
        self.access = access
        self.leaving: set[int] = set()

    async def __call__(self, handler, event: Message, data: dict):
        chat = event.chat
        if chat.type == "private":
            if not event.from_user or not self.access.allowed("user", event.from_user.id):
                return None
        elif chat.type in ("group", "supergroup"):
            if not self.access.allowed("group", chat.id):
                if chat.id not in self.leaving:
                    self.leaving.add(chat.id)
                    try:
                        log.warning("Выход из неразрешённой группы %s (%s)", chat.id, chat.title)
                        await data["bot"].leave_chat(chat.id)
                    except Exception:
                        log.exception("Не удалось выйти из группы %s", chat.id)
                    finally:
                        self.leaving.discard(chat.id)
                return None
        else:
            return None
        return await handler(event, data)


def debug_text(
    db: Storage, tz: ZoneInfo, daily_target: int, owner_id: int,
    access: AccessControl, scheduler: AsyncIOScheduler,
    started_at: datetime, db_path: str,
) -> str:
    """Диагностика только для владельца. Никаких ключей и токенов."""
    now = datetime.now(timezone.utc)
    deadline = now.isoformat()
    counts = {
        "groups": db.conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0],
        "members": db.conn.execute("SELECT COUNT(*) FROM members").fetchone()[0],
        "videos": db.conn.execute("SELECT COUNT(*) FROM video_counts").fetchone()[0],
        "manual": db.conn.execute("SELECT COUNT(*) FROM video_counts WHERE is_manual=1").fetchone()[0],
        "overrides": db.conn.execute("SELECT COUNT(*) FROM daily_overrides").fetchone()[0],
        "reports": db.conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
        "pending_active": db.conn.execute(
            "SELECT COUNT(*) FROM pending_manual WHERE expires_at_utc>?", (deadline,)
        ).fetchone()[0],
        "pending_locked": db.conn.execute(
            "SELECT COUNT(*) FROM pending_manual WHERE invalid_attempts>=? AND expires_at_utc>?",
            (MAX_INVALID_MANUAL_ATTEMPTS, deadline),
        ).fetchone()[0],
        "high_manual_24h": db.conn.execute(
            "SELECT COUNT(*) FROM video_counts WHERE is_manual=1 AND count>=? AND sent_at_utc>=?",
            (MANUAL_REVIEW_THRESHOLD, (now - timedelta(hours=24)).isoformat()),
        ).fetchone()[0],
        "suspicious_24h": db.conn.execute(
            "SELECT COUNT(*) FROM manual_rejections WHERE created_at_utc>=?",
            ((now - timedelta(hours=24)).isoformat(),),
        ).fetchone()[0],
        "pending_expired": db.conn.execute(
            "SELECT COUNT(*) FROM pending_manual WHERE expires_at_utc<=? OR expires_at_utc IS NULL",
            (deadline,),
        ).fetchone()[0],
    }
    try:
        integrity = db.conn.execute("PRAGMA quick_check(1)").fetchone()[0]
    except sqlite3.DatabaseError:
        integrity = "ERROR"
    try:
        db_size = Path(db_path).stat().st_size
    except OSError:
        db_size = 0
    next_report = scheduler.get_job("daily_report")
    next_run = next_report.next_run_time if next_report is not None else None
    uptime = str(timedelta(seconds=int((now - started_at).total_seconds())))
    lines = [
        "🔎 <b>PushUp Tracker: /debug</b>",
        f"🕒 Сейчас: {now.astimezone(tz):%Y-%m-%d %H:%M:%S %Z}",
        f"⏱ Uptime: {uptime}",
        f"🐍 Python: {html.escape(sys.version.split()[0])}",
        f"🗄 SQLite: {sqlite3.sqlite_version} · quick_check: {html.escape(str(integrity))}",
        f"💾 DB: {db_size / 1024 / 1024:.2f} MiB",
        f"🌍 Часовой пояс: {html.escape(tz.key)}",
        f"🎯 Норма: {daily_target}; штрафная: {penalty_target(daily_target)}",
        f"⌛ Ручной ввод: 0–{MAX_MANUAL_COUNT}, "
        f"до {MAX_MANUAL_VIDEOS_PER_DAY} кружочков/сутки, "
        f"{MAX_INVALID_MANUAL_ATTEMPTS} ошибки до блокировки, "
        f"{int(MANUAL_INPUT_TTL.total_seconds() / 60)} минут",
        f"🤖 Gemini (группы): {html.escape(os.getenv('GEMINI_MODEL', DEBUG_GEMINI_MODEL))}",
        f"🧪 Gemini (личка): {html.escape(DEBUG_GEMINI_MODEL)}",
        f"🔑 Gemini API: {'настроен' if os.getenv('GEMINI_API_KEY') else 'не настроен'}",
        f"📅 Следующий отчёт: {next_run.astimezone(tz):%Y-%m-%d %H:%M %Z}" if next_run else "📅 Следующий отчёт: не запланирован",
        "",
        "📊 <b>База данных</b>",
        f"Групп: {counts['groups']}; участников: {counts['members']}",
        f"Обработанных кружочков: {counts['videos']} (вручную: {counts['manual']})",
        f"Корректировок: {counts['overrides']}; отправленных отчётов: {counts['reports']}",
        f"Ожидают ввода: {counts['pending_active']}; блокированы: {counts['pending_locked']}; "
        f"просрочены: {counts['pending_expired']}",
        f"🚩 Попыток ввода числа >{MAX_MANUAL_COUNT} за 24 ч: {counts['suspicious_24h']}",
        f"🔎 Ручных результатов от {MANUAL_REVIEW_THRESHOLD} за 24 ч: {counts['high_manual_24h']}",
        f"Разрешённых групп: {len(access.list_allowed('group'))}",
        f"Пользователей с доступом к личке: {len(access.list_allowed('user'))}",
    ]
    recent_rejections = db.conn.execute(
        """SELECT chat_id, user_id, rejected_count, created_at_utc
           FROM manual_rejections ORDER BY id DESC LIMIT 5"""
    ).fetchall()
    if recent_rejections:
        lines += ["", "🚩 <b>Последние отклонённые ручные числа</b>"]
        lines.extend(
            f"Группа <code>{chat_id}</code>; user <code>{user_id}</code>; "
            f"запрос {rejected}; {html.escape(when[:19])} UTC"
            for chat_id, user_id, rejected, when in recent_rejections
        )
    high_manual = db.conn.execute(
        """SELECT chat_id, user_id, count, sent_at_utc FROM video_counts
           WHERE is_manual=1 AND count>=?
           ORDER BY sent_at_utc DESC LIMIT 5""",
        (MANUAL_REVIEW_THRESHOLD,),
    ).fetchall()
    if high_manual:
        lines += ["", "🔎 <b>Последние высокие ручные результаты</b>"]
        lines.extend(
            f"Группа <code>{chat_id}</code>; user <code>{user_id}</code>; "
            f"{count} повторов; {html.escape(when[:19])} UTC"
            for chat_id, user_id, count, when in high_manual
        )
    last_report = db.conn.execute("SELECT MAX(sent_at_utc) FROM reports").fetchone()[0]
    if last_report:
        lines.append(f"Последний отправленный отчёт: {html.escape(last_report)}")
    details = db.conn.execute(
        """SELECT c.chat_id, c.title, COUNT(DISTINCT m.user_id)
           FROM chats c LEFT JOIN members m ON m.chat_id=c.chat_id
           GROUP BY c.chat_id, c.title ORDER BY c.chat_id LIMIT 10"""
    ).fetchall()
    if details:
        lines += ["", "👥 <b>Группы (первые 10)</b>"]
        lines.extend(
            f"{html.escape(title[:35])} (<code>{chat_id}</code>): {participants} уч."
            for chat_id, title, participants in details
        )
    return "\n".join(lines)


async def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Укажите BOT_TOKEN в .env")
    tz = ZoneInfo(os.getenv("REPORT_TIMEZONE", "Europe/Zurich"))
    try:
        daily_target = int(os.getenv("DAILY_TARGET", "100"))
        cash_fine = int(os.getenv("CASH_FINE", "500"))
    except ValueError as exc:
        raise ValueError("DAILY_TARGET и CASH_FINE должны быть целыми числами") from exc
    if daily_target <= 0:
        raise ValueError("DAILY_TARGET должен быть больше нуля")
    try:
        owner_id = int(os.getenv("OWNER_ID", "0"))
    except ValueError as exc:
        raise ValueError("OWNER_ID должен быть числовым Telegram User ID") from exc
    if owner_id <= 0:
        raise RuntimeError("Укажите OWNER_ID в .env")
    db_path = os.getenv("DB_PATH", "./data/pushups.sqlite3")
    db = Storage(db_path)
    setup_manual_storage(db)
    access = AccessControl(db, owner_id)
    dp = Dispatcher()
    dp.message.outer_middleware(AccessMiddleware(access))
    semaphore = asyncio.Semaphore(2)
    report_lock = asyncio.Lock()
    started_at = datetime.now(timezone.utc)
    rules_path = Path(os.getenv("RULES_PATH", str(Path(__file__).with_name("rules.txt"))))
    scheduler = AsyncIOScheduler(timezone=tz)

    def get_report(chat_id: int, day: date) -> str:
        return report_text(day, daily_report_rows(db, chat_id, day, tz, daily_target), daily_target)

    async with Bot(token=token) as bot:
        async def ask_manual(message: Message, reason: str) -> None:
            if not message.from_user or not message.video_note:
                return
            chat_id, message_id = message.chat.id, message.message_id
            if db.has_video(chat_id, message_id):
                return
            if db.conn.execute(
                "SELECT 1 FROM pending_manual WHERE chat_id=? AND video_message_id=?",
                (chat_id, message_id),
            ).fetchone():
                return
            inserted = create_pending_manual(
                db, chat_id, message_id, message.from_user.id, message.from_user.full_name,
                message.date,
            )
            if not inserted:
                return
            try:
                prompt = await message.reply(
                    f"⚠️ {html.escape(reason)}\n"
                    f"✍️ {html.escape(message.from_user.full_name)}, укажи количество отжиманий "
                    "целым неотрицательным числом в ответ на это сообщение.\n"
                    f"Лимит: {MAX_MANUAL_VIDEOS_PER_DAY} ручных кружочка в сутки, "
                    f"{MAX_INVALID_MANUAL_ATTEMPTS} попытки на ввод.\n"
                    "⏳ На ввод есть 15 минут с момента запроса. "
                    "Результат будет помечен как ручной — его можно перепроверить по кружочку.",
                    parse_mode="HTML",
                    reply_markup=ForceReply(input_field_placeholder="Например: 25", selective=True),
                )
            except Exception:
                with db.conn:
                    db.conn.execute(
                        "DELETE FROM pending_manual WHERE chat_id=? AND video_message_id=?",
                        (chat_id, message_id),
                    )
                log.exception("Не удалось запросить ручной ввод chat=%s msg=%s", chat_id, message_id)
                return
            with db.conn:
                db.conn.execute(
                    "UPDATE pending_manual SET prompt_message_id=? WHERE chat_id=? AND video_message_id=?",
                    (prompt.message_id, chat_id, message_id),
                )

        async def publish_yesterday() -> None:
            async with report_lock:
                yesterday = datetime.now(tz).date() - timedelta(days=1)
                for chat_id in db.chat_ids():
                    if not access.allowed("group", chat_id):
                        continue
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

        def purge_old_pending() -> None:
            """Очищать просроченные запросы и 90-дневные записи об отклонениях."""
            now = datetime.now(timezone.utc)
            threshold = (now - timedelta(days=1)).isoformat()
            history_threshold = (now - timedelta(days=90)).isoformat()
            with db.conn:
                db.conn.execute(
                    "DELETE FROM pending_manual WHERE expires_at_utc<? OR expires_at_utc IS NULL",
                    (threshold,),
                )
                db.conn.execute(
                    "DELETE FROM manual_rejections WHERE created_at_utc<?",
                    (history_threshold,),
                )

        @dp.my_chat_member()
        async def on_bot_membership(event: ChatMemberUpdated) -> None:
            if event.chat.type not in ("group", "supergroup"):
                return
            if event.new_chat_member.status not in ("member", "administrator"):
                return
            if not access.allowed("group", event.chat.id):
                log.warning("Бот добавлен в неразрешённую группу %s", event.chat.id)
                try:
                    await bot.leave_chat(event.chat.id)
                except Exception:
                    log.exception("Не удалось выйти из группы %s", event.chat.id)

        @dp.message(Command("myid"))
        async def on_myid(message: Message) -> None:
            if message.chat.type == "private" and message.from_user:
                await message.answer(
                    f"Твой Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML"
                )

        @dp.message(Command("allowgroup", "denygroup", "allowuser", "denyuser", "access"))
        async def on_access_command(message: Message) -> None:
            if not owner_private(message.chat.type, message.from_user.id if message.from_user else None, owner_id):
                return
            command, *args = (message.text or "").split()
            command = command.split("@")[0].lstrip("/")
            if command == "access":
                groups = ", ".join(map(str, access.list_allowed("group"))) or "нет"
                users = ", ".join(map(str, access.list_allowed("user"))) or "нет"
                await message.answer(f"🔐 Разрешённые группы:\n{groups}\n\n👤 Личный доступ:\n{users}")
                return
            if len(args) != 1:
                await message.answer(f"Использование: /{command} ID")
                return
            try:
                target_id = int(args[0])
            except ValueError:
                await message.answer("ID должен быть целым числом")
                return
            kind = "group" if command.endswith("group") else "user"
            if kind == "user" and target_id <= 0:
                await message.answer("User ID должен быть положительным")
                return
            if kind == "group" and target_id >= 0:
                await message.answer("ID группы должен быть отрицательным")
                return
            enabled = command.startswith("allow")
            try:
                access.set_allowed(kind, target_id, enabled)
            except ValueError as exc:
                await message.answer(str(exc))
                return
            label = "группа" if kind == "group" else "пользователь"
            await message.answer(
                f"{'✅ Разрешено' if enabled else '⛔ Запрещено'}: {label} {target_id}"
            )
            if kind == "group" and not enabled:
                try:
                    await bot.leave_chat(target_id)
                except Exception:
                    log.info("Бот не смог выйти из группы %s (возможно, уже вышел)", target_id)

        @dp.message(Command("start"))
        async def on_start(message: Message) -> None:
            if message.chat.type in ("group", "supergroup"):
                db.register_chat(message.chat.id, message.chat.title or "Группа")
            await message.answer(
                "🏋️ Пришлите видеокружочек с отжиманиями. Я посчитаю повторы.\n"
                "Если видео не распознаётся, можно указать число отжиманий вручную "
                "(15 минут на ввод, до 3 кружочков в сутки).\n"
                f"Дневная норма: {daily_target}. Отчёт — в 00:00 ({tz.key}).\n"
                "Список команд: /help"
            )

        @dp.message(Command("help"))
        async def on_help(message: Message) -> None:
            if message.chat.type in ("group", "supergroup"):
                db.register_chat(message.chat.id, message.chat.title or "Группа")
            await message.answer(help_text(), parse_mode="HTML")

        @dp.message(Command("rules", "pravila"))
        async def on_rules(message: Message) -> None:
            try:
                rules = rules_path.read_text(encoding="utf-8").strip()
            except OSError:
                log.exception("Не удалось прочитать файл правил: %s", rules_path)
                await message.answer("⚠️ Файл правил недоступен. Сообщите администратору.")
                return
            if not rules:
                await message.answer("⚠️ Правила пока не заполнены.")
                return
            variables = {
                "DAILY_TARGET": str(daily_target),
                "REPORT_TIMEZONE": tz.key,
                "PENALTY_TARGET": str(penalty_target(daily_target)),
                "CASH_FINE": str(cash_fine),
            }
            rules = re.sub(
                r"\{(DAILY_TARGET|REPORT_TIMEZONE|PENALTY_TARGET|CASH_FINE)\}",
                lambda match: variables[match.group(1)], rules,
            )
            for offset in range(0, len(rules), 4000):
                await message.answer(rules[offset:offset + 4000], parse_mode=None)

        @dp.message(Command("stats", "today"))
        async def on_today(message: Message) -> None:
            day = datetime.now(tz).date()
            rows = daily_report_rows(db, message.chat.id, day, tz, daily_target)
            await message.answer(today_text(day, rows), parse_mode="HTML")

        @dp.message(Command("yesterday"))
        async def on_yesterday(message: Message) -> None:
            day = datetime.now(tz).date() - timedelta(days=1)
            await message.answer(get_report(message.chat.id, day), parse_mode="HTML")

        @dp.message(Command("debug"))
        async def on_debug(message: Message) -> None:
            if not owner_private(message.chat.type, message.from_user.id if message.from_user else None, owner_id):
                return
            text = debug_text(db, tz, daily_target, owner_id, access, scheduler, started_at, db_path)
            await message.answer(text, parse_mode="HTML")

        @dp.message(Command("setpushups", "clearpushups"))
        async def on_override_pushups(message: Message) -> None:
            """Владелец задаёт/удаляет дневную корректировку (как и раньше)."""
            if not message.from_user or message.from_user.id != owner_id:
                await message.reply("🔒 Команда доступна только владельцу бота.")
                return
            if message.chat.type not in ("private", "group", "supergroup"):
                return
            command, *args = (message.text or "").split()
            command = command.split("@")[0].lstrip("/")
            setting = command == "setpushups"
            chosen_day = datetime.now(tz).date()
            if args and re.fullmatch(r"\d{4}-\d{2}-\d{2}", args[-1]):
                try:
                    chosen_day = date.fromisoformat(args.pop())
                except ValueError:
                    await message.reply("❌ Неверная дата, нужен формат YYYY-MM-DD.")
                    return
            is_private = message.chat.type == "private"
            reply_user = (
                message.reply_to_message.from_user
                if not is_private and message.reply_to_message else None
            )
            if reply_user and getattr(reply_user, "is_bot", False):
                await message.reply("❌ Ответь на сообщение участника, а не бота.")
                return
            try:
                if is_private:
                    expected = 3 if setting else 2
                    if len(args) != expected:
                        raise ValueError("Неверное число аргументов")
                    chat_id, user_id = int(args[0]), int(args[1])
                    raw_count = args[2] if setting else None
                elif reply_user:
                    expected = 1 if setting else 0
                    if len(args) != expected:
                        raise ValueError("Неверное число аргументов")
                    chat_id, user_id = message.chat.id, reply_user.id
                    raw_count = args[0] if setting else None
                else:
                    expected = 2 if setting else 1
                    if len(args) != expected:
                        raise ValueError("Неверное число аргументов")
                    chat_id, user_id = message.chat.id, int(args[0])
                    raw_count = args[1] if setting else None
                if chat_id >= 0 or user_id <= 0:
                    raise ValueError("ID группы должен быть отрицательным, ID участника — положительным")
                count = None
                if setting:
                    if raw_count is None or not re.fullmatch(r"[0-9]{1,6}", raw_count):
                        raise ValueError("Количество должно быть целым неотрицательным числом")
                    count = int(raw_count)
                    if count > MAX_DAILY_OVERRIDE:
                        raise ValueError(f"Максимум — {MAX_DAILY_OVERRIDE}")
            except ValueError as exc:
                await message.reply(
                    f"❌ {html.escape(str(exc))}\n\n"
                    "<b>В ответ на сообщение в группе:</b>\n"
                    "<code>/setpushups 120 [YYYY-MM-DD]</code>\n"
                    "<code>/clearpushups [YYYY-MM-DD]</code>\n\n"
                    "<b>В группе по ID:</b>\n"
                    "<code>/setpushups USER_ID 120 [YYYY-MM-DD]</code>\n"
                    "<code>/clearpushups USER_ID [YYYY-MM-DD]</code>\n\n"
                    "<b>В личке:</b>\n"
                    "<code>/setpushups CHAT_ID USER_ID 120 [YYYY-MM-DD]</code>\n"
                    "<code>/clearpushups CHAT_ID USER_ID [YYYY-MM-DD]</code>",
                    parse_mode="HTML",
                )
                return
            if not is_private:
                db.register_chat(chat_id, message.chat.title or "Группа")
            elif chat_id not in db.chat_ids():
                await message.reply("❌ Группа не зарегистрирована в базе бота.")
                return
            display_name = reply_user.full_name if reply_user else db.member_name(chat_id, user_id)
            if display_name is None:
                try:
                    member = await bot.get_chat_member(chat_id, user_id)
                    if getattr(member.status, "value", member.status) in ("left", "kicked"):
                        raise ValueError("Пользователь не состоит в группе")
                    display_name = member.user.full_name
                except Exception:
                    await message.reply(
                        "❌ Не могу определить пользователя по ID. "
                        "Ответь командой на его сообщение в группе или укажи известный ID участника."
                    )
                    return
            if setting:
                db.set_daily_override(chat_id, user_id, display_name, chosen_day, count, message.from_user.id)
                action_text = f"🛠️ Установлено <b>{count}</b> отжиманий"
            else:
                deleted = db.clear_daily_override(chat_id, user_id, chosen_day)
                if not deleted:
                    await message.reply("ℹ️ На указанную дату корректировка отсутствует.")
                    return
                action_text = "✅ Корректировка удалена, снова учитываются видео"
            current = db.daily_user_total(chat_id, user_id, chosen_day, tz)
            detail = f"\n📊 Сейчас за день: <b>{current}</b>" if current is not None else ""
            log.info(
                "Дневная корректировка: command=%s chat=%s user=%s day=%s count=%s admin=%s",
                command, chat_id, user_id, chosen_day, count, message.from_user.id,
            )
            await message.reply(
                f"{action_text}\n👤 {html.escape(display_name)}\n"
                f"📅 {chosen_day:%d.%m.%Y}{detail}\n"
                "ℹ️ Изменение учтётся в /stats и /yesterday.",
                parse_mode="HTML",
            )

        @dp.message(F.text, F.reply_to_message)
        async def on_manual_reply(message: Message) -> None:
            if not message.from_user or message.from_user.is_bot or not message.reply_to_message:
                return
            result = process_manual_reply(
                db, message.chat.id, message.reply_to_message.message_id,
                message.from_user.id, message.text, report_tz=tz,
            )
            if result.notify_owner:
                if result.status == "accepted":
                    owner_notice = (
                        f"🔎 Высокий ручной результат: user {message.from_user.id}, "
                        f"чат {message.chat.id}, ответ на сообщение {message.reply_to_message.message_id}: "
                        f"{result.count} повторов. Результат засчитан, но рекомендуется "
                        "проверить исходное видео (/debug показывает последние такие записи)."
                    )
                else:
                    owner_notice = (
                        f"🚩 Превышение лимита ручного ввода: user {message.from_user.id}, "
                        f"чат {message.chat.id}, запрос {message.reply_to_message.message_id}: "
                        f"{result.count} повторов. Максимум — {MAX_MANUAL_COUNT}. "
                        "Результат не засчитан. Случай записан в /debug."
                    )
                try:
                    await bot.send_message(owner_id, owner_notice)
                except Exception:
                    log.warning("Не удалось уведомить владельца о ручном вводе chat=%s user=%s",
                                message.chat.id, message.from_user.id)
            if result.status == "not_found":
                return
            if result.status == "forbidden":
                await message.reply("🔒 Количество может указать только автор кружочка.")
            elif result.status == "expired":
                await message.reply("⌛ Время ручного ввода истекло (15 минут). Отправь новый кружочек.")
            elif result.status in ("invalid", "too_large"):
                # Не сообщаем участнику порог и не различаем ошибки формата и превышение.
                # Внутренняя причина и подозрительное значение остаются в журнале владельца.
                await message.reply(manual_input_error_message(result.attempts_left))
            elif result.status == "locked":
                await message.reply(
                    f"🔒 После {MAX_INVALID_MANUAL_ATTEMPTS} ошибочных попыток ручной ввод "
                    "для этого кружочка заблокирован. Пришли новый кружочек или обратись к владельцу."
                )
            elif result.status == "daily_limit":
                await message.reply(
                    f"🛑 На сегодня исчерпан лимит: {MAX_MANUAL_VIDEOS_PER_DAY} "
                    "кружочка с ручным вводом. Отправь видео для распознавания "
                    "или обратись к владельцу."
                )
            elif result.status == "accepted":
                await message.reply(
                    f"✍️ <b>{html.escape(result.name)}</b>: <b>{result.count}</b> отжиманий — "
                    "указано вручную, можно перепроверить по видео.",
                    parse_mode="HTML",
                )

        @dp.message(F.video_note)
        async def on_video_note(message: Message) -> None:
            if message.chat.type == "private":
                # Диагностика видео в личке: не сохраняет статистику и не просит ручной ввод.
                note = message.video_note
                details = [
                    "🔎 <b>Gemini: диагностика видео</b>",
                    f"🧠 Модель отладки: <code>{DEBUG_GEMINI_MODEL}</code>",
                    f"🎬 Длительность видео: {note.duration} с",
                    (f"💾 Размер: {note.file_size / (1024 * 1024):.2f} МБ"
                     if note.file_size is not None else "💾 Размер: неизвестен"),
                    f"📦 Локальный лимит файла: {MAX_DOWNLOAD_BYTES / (1024 * 1024):.0f} МБ",
                    "🌐 Таймаут скачивания: 90 с",
                    "🧵 Одновременно анализируется до 2 видео",
                ]
                if note.file_size and note.file_size > MAX_DOWNLOAD_BYTES:
                    details += ["❌ Файл превышает локальный лимит — анализ не выполнялся.",
                                "🧪 Результат не записан в статистику."]
                    await message.answer("\n".join(details), parse_mode="HTML")
                    return
                started = time.perf_counter()
                queue_seconds = download_seconds = analysis_seconds = None
                phase = "ожидание свободного слота"
                analysis = error = None
                try:
                    async with semaphore:
                        queue_seconds = time.perf_counter() - started
                        with tempfile.TemporaryDirectory(prefix="pushup-debug-") as tempdir:
                            target = Path(tempdir) / "note.mp4"
                            phase = "скачивание из Telegram"
                            download_started = time.perf_counter()
                            try:
                                await bot.download(note.file_id, destination=target, timeout=90)
                            finally:
                                download_seconds = time.perf_counter() - download_started
                            phase = "анализ Gemini"
                            analysis_started = time.perf_counter()
                            try:
                                analysis = await asyncio.to_thread(
                                    analyze_video, target, duration_seconds=note.duration,
                                    model_override=DEBUG_GEMINI_MODEL,
                                )
                            finally:
                                analysis_seconds = time.perf_counter() - analysis_started
                except Exception as exc:
                    error = exc
                    log.exception("Ошибка debug-анализа кружочка user=%s phase=%s",
                                  message.from_user.id if message.from_user else "unknown", phase)
                details.append("\n⏱ <b>Замеры времени</b>")
                if queue_seconds is not None:
                    details.append(f"⏳ Ожидание слота: {queue_seconds:.2f} с")
                if download_seconds is not None:
                    details.append(f"📥 Скачивание: {download_seconds:.2f} с")
                if analysis_seconds is not None:
                    details.append(f"🤖 Анализ Gemini: {analysis_seconds:.2f} с")
                details.append(f"🕒 Всего: {time.perf_counter() - started:.2f} с")
                if error is not None:
                    details.append(f"❌ Ошибка на этапе «{phase}» ({type(error).__name__}). Подробности — в логах.")
                elif analysis is None:
                    details.append("⚠️ Анализатор не вернул надёжный результат.")
                else:
                    details.append("\n💪 <b>Результат</b>")
                    details.append(f"Отжиманий: <b>{analysis.repetitions}</b>")
                    details.append(f"Неопределённость: {'⚠️ да' if analysis.uncertain else '✅ нет'}")
                    timestamps = analysis.completed_at_seconds
                    details.append(
                        "Моменты завершения: " + (
                            ", ".join(f"{timestamp:g} с" for timestamp in timestamps[:100])
                            + (f" …и ещё {len(timestamps) - 100}" if len(timestamps) > 100 else "")
                            if timestamps else "нет"
                        )
                    )
                    if analysis.requested_fps is not None:
                        details.append(f"🎞 Заданная частота выборки видео: {analysis.requested_fps} FPS")
                    if analysis.api_seconds is not None:
                        details.append(f"⚙️ Вызов Gemini API: {analysis.api_seconds:.2f} с")
                    details.append(
                        "⚠️ Gemini сообщил о неопределённости: результат не подтверждён."
                        if analysis.uncertain else "✅ Ответ анализатора получен."
                    )
                    details.append("\n🔢 <b>Использование токенов</b>")
                    usage = analysis.usage_metadata
                    token_fields = (
                        ("Вход", "prompt_token_count"),
                        ("Выход", "candidates_token_count"),
                        ("Thinking", "thoughts_token_count"),
                        ("Из кеша", "cached_content_token_count"),
                        ("Всего", "total_token_count"),
                    )
                    if usage:
                        for label, key in token_fields:
                            value = usage.get(key)
                            if value is not None:
                                details.append(f"{label}: {value:,}".replace(",", " "))
                    else:
                        details.append("Метаданные токенов не предоставлены Gemini API.")
                    if analysis.model:
                        details.append(f"🧠 Модель: {html.escape(analysis.model)}")
                    if analysis.finish_reason:
                        details.append(f"🛑 Причина завершения: {html.escape(analysis.finish_reason)}")
                details.append("🧪 Тестовый режим: результат не записан в статистику.")
                await message.answer("\n".join(details), parse_mode="HTML")
                return
            if message.chat.type not in ("group", "supergroup"):
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
                        analysis = await asyncio.to_thread(
                            analyze_video, target, duration_seconds=note.duration,
                        )
            except Exception:
                log.exception("Ошибка анализа кружочка chat=%s msg=%s", message.chat.id, message.message_id)
                await ask_manual(message, "Не удалось обработать этот кружочек автоматически.")
                return
            if analysis is None or analysis.uncertain:
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

        scheduler.add_job(
            publish_yesterday, "cron", hour=0, minute=0,
            id="daily_report", max_instances=1, coalesce=True,
        )
        scheduler.add_job(purge_old_pending, "interval", hours=1,
                          id="clean_manual_pending", max_instances=1, coalesce=True)
        scheduler.start()
        try:
            try:
                await bot.set_my_commands(PUBLIC_COMMANDS)
            except Exception:
                log.exception("Не удалось обновить список команд Telegram")
            await bot.delete_webhook(drop_pending_updates=False)
            await publish_yesterday()
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            scheduler.shutdown(wait=False)
            db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
