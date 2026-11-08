from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, ForceReply, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from storage import Storage
from vision import analyze_video

log = logging.getLogger(__name__)
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_MANUAL_COUNT = 1000
MAX_DAILY_OVERRIDE = 100_000
DEBUG_GEMINI_MODEL = "gemini-3.5-flash-lite"


def setup_manual_storage(db: Storage) -> None:
    """Дополнить исходную схему SQLite, не изменяя storage.py."""
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


def parse_id_list(raw: str, label: str) -> set[int]:
    """Разобрать список ID из .env, не допуская ошибочную конфигурацию."""
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
        # Решение владельца из БД имеет приоритет над начальными значениями .env.
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
                """INSERT INTO access_rules(kind, target_id, enabled) VALUES(?, ?, ?)
                   ON CONFLICT(kind, target_id) DO UPDATE SET enabled=excluded.enabled""",
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
    """Проверять доступ ДО выполнения любых обработчиков сообщения."""

    def __init__(self, access: AccessControl) -> None:
        self.access = access
        self.leaving: set[int] = set()

    async def __call__(self, handler, event: Message, data: dict):
        chat = event.chat
        if chat.type == "private":
            if not event.from_user or not self.access.allowed("user", event.from_user.id):
                return None  # Посторонним в личке ничего не отвечаем.
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


def daily_report_rows(
    db: Storage, chat_id: int, day: date, tz: ZoneInfo, daily_target: int = 100
) -> list[tuple[str, int, int, int, int, bool]]:
    """(имя, повторы, видео, ручные видео, цель, override).

    Админская корректировка переопределяет итог дня, но не число кружочков.
    Штрафные цели пересчитываются с учётом корректировок прошлых дней.
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
        member["first"] = min(member["first"], video_day)
        totals = member["days"].setdefault(video_day, [0, 0, 0, False])
        totals[0] += count
        totals[1] += 1
        totals[2] += manual

    # Переопределение применяется ПОСЛЕ суммирования видео.
    # Оно меняет дневной результат, а не количество записанных видео.
    for user_id, name, override_day, override_count in db.daily_overrides_until(chat_id, day):
        member = members.setdefault(
            user_id, {"name": name, "first": override_day, "days": {}}
        )
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
            goal = daily_target if done >= goal else daily_target * 2 + 10
            current_day += timedelta(days=1)
        reps, videos, manual, overridden = member["days"].get(day, [0, 0, 0, False])
        result.append((member["name"], reps, videos, manual, goal, overridden))
    return result


def report_text(
    day: date, rows: list[tuple[str, int, int, int, int, bool]], target: int = 100
) -> str:
    lines = [
        f"🏋️ <b>Отжимания за {day:%d.%m.%Y}</b>",
        f"🎯 Обычная норма: <b>{target}</b> отжиманий",
        "",
    ]
    for name, count, videos, manual, goal, overridden in rows:
        status = "✅" if count >= goal else "❌"
        suffix = f" · ✍️ {manual} вручную" if manual else ""
        if overridden:
            suffix += " · 🛠️ скорректировано"
        lines.append(f"{status} <b>{html.escape(name)}</b> — {count}/{goal} · 🎥 {videos}{suffix}")
        if goal > target:
            lines.append(f"   🔥 <b>СЕГОДНЯ ШТРАФНАЯ НОРМА: {goal}!</b>")
        if count < goal:
            lines.append(f"   🚨 <b>НА {day + timedelta(days=1):%d.%m} НАЗНАЧЕНА НОРМА ×2 + 10: {target * 3}!</b>")
    if not rows:
        lines.append("Пока нет зарегистрированных участников с кружочками или корректировками.")
    if any(manual for _, _, _, manual, _, _ in rows):
        lines += ["", "✍️ Результаты введены вручную — их можно перепроверить по видео."]
    if any(overridden for _, _, _, _, _, overridden in rows):
        lines += ["", "🛠️ Итог за день задан администратором (вместо суммы по видео)."]
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
        raise ValueError("DAILY_TARGET должен быть целым положительным числом") from exc
    if daily_target <= 0:
        raise ValueError("DAILY_TARGET должен быть больше нуля")
    try:
        owner_id = int(os.getenv("OWNER_ID", "0"))
    except ValueError as exc:
        raise ValueError("OWNER_ID должен быть числовым Telegram User ID") from exc
    if owner_id <= 0:
        raise RuntimeError("Укажите OWNER_ID в .env: бот работает только с заданным владельцем")

    db = Storage(os.getenv("DB_PATH", "./data/pushups.sqlite3"))
    setup_manual_storage(db)
    access = AccessControl(db, owner_id)
    dp = Dispatcher()
    dp.message.outer_middleware(AccessMiddleware(access))
    semaphore = asyncio.Semaphore(2)
    report_lock = asyncio.Lock()

    # Текст правил хранится отдельно и перечитывается при каждом вызове команды.
    rules_path = Path(os.getenv("RULES_PATH", str(Path(__file__).with_name("rules.txt"))))

    def get_report(chat_id: int, day: date) -> str:
        return report_text(day, daily_report_rows(db, chat_id, day, tz, daily_target), daily_target)

    async with Bot(token=token) as bot:
        async def ask_manual(message: Message, reason: str) -> None:
            """Сохранить запрос до отправки сообщения с просьбой о ручном вводе."""
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

        @dp.my_chat_member()
        async def on_bot_membership(event: ChatMemberUpdated) -> None:
            """Сразу покинуть любую новую группу, которой нет в белом списке."""
            if event.chat.type not in ("group", "supergroup"):
                return
            if event.new_chat_member.status not in ("member", "administrator"):
                return
            if not access.allowed("group", event.chat.id):
                log.warning(
                    "Бот добавлен в чужую группу: %s (%s). Для разрешения: /allowgroup %s",
                    event.chat.id, event.chat.title, event.chat.id,
                )
                try:
                    await bot.leave_chat(event.chat.id)
                except Exception:
                    log.exception("Не удалось выйти из неразрешённой группы %s", event.chat.id)

        @dp.message(Command("myid"))
        async def on_myid(message: Message) -> None:
            if message.chat.type == "private":
                await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")

        @dp.message(Command("allowgroup", "denygroup", "allowuser", "denyuser", "access"))
        async def on_access_command(message: Message) -> None:
            """Управление доступом — только владелец и только в личке."""
            if message.chat.type != "private" or not message.from_user or message.from_user.id != owner_id:
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
            await message.answer(f"{'✅ Разрешено' if enabled else '⛔ Запрещено'}: {label} {target_id}")
            if kind == "group" and not enabled:
                try:
                    await bot.leave_chat(target_id)
                except Exception:
                    log.info("Бот не смог выйти из группы %s (возможно, уже вышел)", target_id)

        @dp.message(Command("start", "help"))
        async def on_start(message: Message) -> None:
            if message.chat.type in ("group", "supergroup"):
                db.register_chat(message.chat.id, message.chat.title or "Группа")
            await message.answer(
                "🏋️ Пришлите видеокружочек с отжиманиями. Я посчитаю повторы и отмечу выполнение дневной нормы.\n"
                "Если видео не распознаётся, попрошу автора написать число вручную. "
                "Такие результаты помечаются ✍️ и их можно проверить.\n"
                "Команды: /stats — сегодня, /yesterday — вчера, /rules — правила. "
                f"Дневная норма: {daily_target}. Отчёт — в 00:00 ({tz.key})."
                + ("\nВладелец: /setpushups, /clearpushups — ручная корректировка итогов за день."
                   if message.from_user and message.from_user.id == owner_id else "")
            )

        @dp.message(Command("rules", "pravila"))
        async def on_rules(message: Message) -> None:
            """Показать правила челленджа из отдельного текстового файла."""
            try:
                rules = rules_path.read_text(encoding="utf-8").strip()
            except OSError:
                log.exception("Не удалось прочитать файл правил: %s", rules_path)
                await message.answer("⚠️ Файл правил недоступен. Сообщите администратору.")
                return
            if not rules:
                await message.answer("⚠️ Правила пока не заполнены.")
                return

            # Подставляем только разрешённые переменные, не раскрывая секреты из .env.
            variables = {
                "DAILY_TARGET": str(daily_target),
                "REPORT_TIMEZONE": tz.key,
                "PENALTY_TARGET": str(daily_target * 2 + 10),
                "CASH_FINE": str(cash_fine),
            }
            rules = re.sub(
                r"\{(DAILY_TARGET|REPORT_TIMEZONE|PENALTY_TARGET|CASH_FINE)\}",
                lambda match: variables[match.group(1)],
                rules,
            )
            # Без parse_mode: текст файла выводится как есть, без проблем с HTML.
            # Telegram ограничивает размер сообщения 4096 символами.
            for start in range(0, len(rules), 4000):
                await message.answer(rules[start:start + 4000], parse_mode=None)

        @dp.message(Command("stats", "yesterday"))
        async def on_stats(message: Message) -> None:
            day = datetime.now(tz).date()
            if message.text and message.text.split()[0].split("@")[0] == "/yesterday":
                day -= timedelta(days=1)
            await message.answer(get_report(message.chat.id, day), parse_mode="HTML")

        @dp.message(Command("setpushups", "clearpushups"))
        async def on_override_pushups(message: Message) -> None:
            """Владелец задаёт/снимает итог дня для одного участника.

            Ответ на сообщение участника в группе:
                /setpushups COUNT [YYYY-MM-DD]
                /clearpushups [YYYY-MM-DD]
            В группе по ID:
                /setpushups USER_ID COUNT [YYYY-MM-DD]
                /clearpushups USER_ID [YYYY-MM-DD]
            В личке владельца:
                /setpushups CHAT_ID USER_ID COUNT [YYYY-MM-DD]
                /clearpushups CHAT_ID USER_ID [YYYY-MM-DD]
            """
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
                    if raw_count is None or not re.fullmatch(r"[0-9]+", raw_count):
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
                # Не записываем произвольные ID, которых нет среди участников.
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
                db.set_daily_override(
                    chat_id, user_id, display_name, chosen_day, count, message.from_user.id
                )
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
                f"{action_text}\n"
                f"👤 {html.escape(display_name)} (<code>{user_id}</code>)\n"
                f"📅 {chosen_day:%d.%m.%Y} · чат <code>{chat_id}</code>"
                f"{detail}\n"
                "ℹ️ Изменение учтётся в /stats и /yesterday. Уже отправленные отчёты не пересылаются.",
                parse_mode="HTML",
            )

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
            # Между сохранением результата и удалением ожидающего запроса нет await, чтобы избежать дублей.
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
            # В личном чате анализируем кружочек отдельно от статистики группы.
            # Ни запись в SQLite, ни запрос ручного ввода в debug-режиме не создаются.
            if message.chat.type == "private":
                note = message.video_note
                details = [
                    "🔎 <b>Gemini: диагностика видео</b>",
                    f"🧠 Модель отладки: <code>{DEBUG_GEMINI_MODEL}</code>",
                    f"🎬 Длительность видео: {note.duration} с",
                    (
                        f"💾 Размер: {note.file_size / (1024 * 1024):.2f} МБ"
                        if note.file_size is not None else "💾 Размер: неизвестен"
                    ),
                    f"📦 Локальный лимит файла: {MAX_DOWNLOAD_BYTES / (1024 * 1024):.0f} МБ",
                    "🌐 Таймаут скачивания: 90 с",
                    "🧵 Одновременно анализируется до 2 видео",
                ]
                if note.file_size and note.file_size > MAX_DOWNLOAD_BYTES:
                    details.append("❌ Файл превышает локальный лимит — анализ не выполнялся.")
                    details.append("🧪 Результат не записан в статистику.")
                    await message.answer("\n".join(details), parse_mode="HTML")
                    return

                started = time.perf_counter()
                queue_seconds = None
                download_seconds = None
                analysis_seconds = None
                phase = "ожидание свободного слота"
                analysis = None
                error = None
                try:
                    # Общий semaphore такой же, как в группах.
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
                                    model_override=DEBUG_GEMINI_MODEL
                                )
                            finally:
                                analysis_seconds = time.perf_counter() - analysis_started
                except Exception as exc:
                    error = exc
                    log.exception(
                        "Ошибка debug-анализа кружочка user=%s phase=%s",
                        message.from_user.id if message.from_user else "unknown",
                        phase,
                    )

                details.append("\n⏱ <b>Замеры времени</b>")
                if queue_seconds is not None:
                    details.append(f"⏳ Ожидание слота: {queue_seconds:.2f} с")
                if download_seconds is not None:
                    details.append(f"📥 Скачивание: {download_seconds:.2f} с")
                if analysis_seconds is not None:
                    details.append(f"🤖 Анализ Gemini: {analysis_seconds:.2f} с")
                details.append(f"🕒 Всего: {time.perf_counter() - started:.2f} с")

                if error is not None:
                    details.append(
                        f"❌ Ошибка на этапе «{phase}» ({type(error).__name__}). "
                        "Подробности — в логах."
                    )
                elif analysis is None:
                    details.append("⚠️ Анализатор не вернул надёжный результат.")
                else:
                    details.append("\n💪 <b>Результат</b>")
                    details.append(f"Отжиманий: <b>{analysis.repetitions}</b>")
                    details.append(
                        f"Неопределённость: {'⚠️ да' if analysis.uncertain else '✅ нет'}"
                    )
                    details.append(
                        "Моменты завершения: "
                        + (
                            ", ".join(f"{timestamp:g} с" for timestamp in analysis.completed_at_seconds)
                            if analysis.completed_at_seconds
                            else "нет"
                        )
                    )
                    if analysis.requested_fps is not None:
                        details.append(
                            f"🎞 Заданная частота выборки видео: {analysis.requested_fps} FPS"
                        )
                    if analysis.api_seconds is not None:
                        details.append(f"⚙️ Вызов Gemini API: {analysis.api_seconds:.2f} с")
                    details.append(
                        "⚠️ Gemini сообщил о неопределённости: результат не подтверждён."
                        if analysis.uncertain
                        else "✅ Ответ анализатора получен."
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
                        details.append(
                            f"🛑 Причина завершения: {html.escape(analysis.finish_reason)}"
                        )

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
                            analyze_video, target, duration_seconds=note.duration
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
