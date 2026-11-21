"""Rich HTML-карточки групповых кружочков и действия их авторов."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, ForceReply, Message, ReplyParameters

from rich_messages import EditRichMessage, SendRichMessage, render_video_card

from storage import Storage, VideoJob, video_action_flags
from tracker_core import (
    MAX_INVALID_MANUAL_ATTEMPTS, MAX_MANUAL_VIDEOS_PER_DAY, create_pending_manual,
)
from vision import MAX_VIDEO_BYTES, ModelOverloadedError, analyze_video

log = logging.getLogger(__name__)
BUSY_STATES = {"queued", "downloading", "analyzing"}


class VideoWorkflow:
    def __init__(
        self, bot: Bot, db: Storage, access, tz: ZoneInfo,
        semaphore: asyncio.Semaphore, fallback_model: str,
    ) -> None:
        self.bot = bot
        self.db = db
        self.access = access
        self.tz = tz
        self.semaphore = semaphore
        self.fallback_model = fallback_model
        self._active: set[tuple[int, int]] = set()
        self._widget_locks: dict[tuple[int, int], asyncio.Lock] = {}

    def chat_allowed(self, chat_type: str, chat_id: int, user_id: int) -> bool:
        """Проверять разрешения для групп и лички единообразно."""
        if chat_type == "private":
            return self.access.allowed("user", user_id)
        if chat_type in ("group", "supergroup"):
            return self.access.allowed("group", chat_id)
        return False

    def job_allowed(self, job: VideoJob) -> bool:
        """Личный чат Telegram имеет chat_id == user_id."""
        if job.chat_id > 0 and job.chat_id == job.user_id:
            return self.access.allowed("user", job.user_id)
        return self.access.allowed("group", job.chat_id)

    @staticmethod
    def expired(job: VideoJob) -> bool:
        return bool(
            job.action_expires_at_utc
            and datetime.now(timezone.utc) >= datetime.fromisoformat(job.action_expires_at_utc)
        )

    async def refresh_widget(self, chat_id: int, message_id: int, note: str = "") -> None:
        """Ошибка Telegram не отменяет сохранение результата или сам анализ."""
        key = (chat_id, message_id)
        lock = self._widget_locks.setdefault(key, asyncio.Lock())
        async with lock:
            for attempt in range(3):
                job = self.db.get_video_job(*key)
                if job is None:
                    return
                result = self.db.get_video_result(*key)
                content, actions_open = render_video_card(job, result, self.tz, note)
                try:
                    if job.widget_message_id is None:
                        widget = await self.bot(SendRichMessage(
                            chat_id=chat_id, rich_message=content,
                            message_thread_id=job.message_thread_id,
                            reply_parameters=ReplyParameters(message_id=message_id, allow_sending_without_reply=True),
                        ))
                        self.db.bind_video_widget(*key, widget.message_id)
                    else:
                        await self.bot(EditRichMessage(
                            chat_id=chat_id, message_id=job.widget_message_id,
                            rich_message=content,
                        ))
                    self._mark_rendered(job, result, actions_open)
                    return
                except TelegramRetryAfter as exc:
                    if attempt < 2:
                        await asyncio.sleep(exc.retry_after)
                        continue
                    log.warning("Не удалось обновить карточку: flood control chat=%s msg=%s", *key)
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc).lower():
                        self._mark_rendered(job, result, actions_open)
                        return
                    if job.widget_message_id is not None and any(
                        reason in str(exc).lower()
                        for reason in ("message to edit not found", "message can't be edited")
                    ):
                        self.db.bind_video_widget(*key, None)
                        continue
                    log.warning("Не удалось обновить карточку chat=%s msg=%s: %s", *key, exc)
                except TelegramAPIError:
                    log.exception("Не удалось обновить карточку chat=%s msg=%s", *key)
                return

    def _mark_rendered(self, job: VideoJob, result, actions_open: bool) -> None:
        key = (job.chat_id, job.message_id)
        current = self.db.get_video_job(*key)
        if current is None:
            return
        # Ручной ответ мог изменить результат, пока Telegram принимал editMessageText.
        if (
            current.state, current.error, current.action_expires_at_utc,
            current.retry_used, current.result_source, current.result_locked,
            self.db.get_video_result(*key),
        ) != (
            job.state, job.error, job.action_expires_at_utc,
            job.retry_used, job.result_source, job.result_locked, result,
        ):
            return
        self.db.mark_video_widget_clean(*key)
        # Блокировка производится в БД независимо от успешного Telegram edit.

    async def on_video(self, message: Message) -> None:
        if not message.from_user or message.from_user.is_bot or not message.video_note:
            return
        if not self.chat_allowed(message.chat.type, message.chat.id, message.from_user.id):
            return
        key = (message.chat.id, message.message_id)
        if self.db.has_video(*key) or self.db.get_video_job(*key):
            return
        note = message.video_note
        # Отчёты по группам не должны включать личные диалоги.
        if message.chat.type in ("group", "supergroup"):
            self.db.register_chat(message.chat.id, message.chat.title or "Группа")
        self.db.create_video_job(
            *key, message.from_user.id, message.from_user.full_name,
            note.file_id, note.duration, note.file_size, message.date,
            message_thread_id=message.message_thread_id,
        )
        self._active.add(key)
        try:
            await self.refresh_widget(*key)
            await self._process(*key)
        finally:
            self._active.discard(key)

    async def _process(self, chat_id: int, message_id: int) -> None:
        key = (chat_id, message_id)
        job = self.db.get_video_job(*key)
        if job is None:
            return
        analysis = None
        source = None
        error = "Не удалось уверенно распознать отжимания."
        is_retry = job.retry_used > 0
        try:
            if job.file_size and job.file_size > MAX_VIDEO_BYTES:
                error = "Кружочек превышает лимит скачивания (20 МБ)."
            else:
                async with self.semaphore:
                    self.db.set_video_job_state(*key, "downloading")
                    await self.refresh_widget(*key)
                    with tempfile.TemporaryDirectory(prefix="pushup-") as tempdir:
                        target = Path(tempdir) / "note.mp4"
                        await self.bot.download(job.file_id, destination=target, timeout=90)
                        self.db.set_video_job_state(*key, "analyzing")
                        await self.refresh_widget(*key)

                        # Каждая попытка сначала использует сильную модель.
                        # Слабую пробуем только при первоначальном анализе.
                        try:
                            primary = await asyncio.to_thread(
                                analyze_video, target, duration_seconds=job.duration,
                            )
                        except ModelOverloadedError:
                            primary = None
                            log.warning("Сильная модель перегружена chat=%s msg=%s", *key)

                        if primary is not None and not primary.uncertain:
                            analysis = primary
                            source = "strong"
                        elif not is_retry:
                            primary_name = os.getenv(
                                "GEMINI_MODEL", "gemini-3.5-flash-lite"
                            ).strip()
                            if primary_name != self.fallback_model:
                                await self.refresh_widget(
                                    *key, note="Пробую резервную модель…",
                                )
                                try:
                                    fallback = await asyncio.to_thread(
                                        analyze_video, target, duration_seconds=job.duration,
                                        model_override=self.fallback_model,
                                    )
                                except ModelOverloadedError:
                                    fallback = None
                                    log.warning(
                                        "Резервная модель перегружена chat=%s msg=%s", *key
                                    )
                                if fallback is not None and not fallback.uncertain:
                                    analysis = fallback
                                    source = "weak"
        except Exception:
            log.exception("Ошибка обработки кружочка chat=%s msg=%s", *key)
            error = "Не удалось обработать видео."

        self.db.complete_video_analysis(
            *key,
            count=analysis.repetitions if analysis is not None else None,
            source=source,
            error=error,
        )
        await self.refresh_widget(*key)

    @staticmethod
    async def _answer(query: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
        try:
            await query.answer(text, show_alert=alert)
        except TelegramAPIError:
            log.warning("Не удалось подтвердить нажатие кнопки", exc_info=True)

    async def on_callback(self, query: CallbackQuery) -> None:
        match = re.fullmatch(r"video:(manual|retry):([0-9]{1,20})", query.data or "")
        if match is None or not isinstance(query.message, Message):
            await self._answer(query, "Карточка недоступна.", True)
            return
        chat_id = query.message.chat.id
        if not self.chat_allowed(query.message.chat.type, chat_id, query.from_user.id):
            await self._answer(query, "В этом чате действие недоступно.", True)
            return
        action, original_id = match.groups()
        message_id = int(original_id)
        if not 0 < message_id <= 2**63 - 1:
            await self._answer(query, "Карточка недоступна.", True)
            return
        key = (chat_id, message_id)
        job = self.db.get_video_job(*key)
        if job is None or job.widget_message_id != query.message.message_id:
            await self._answer(query, "Карточка устарела или недоступна.", True)
            return
        if query.from_user.id != job.user_id:
            await self._answer(query, "Изменить результат может только автор кружочка.", True)
            return
        if key in self._active or job.state in BUSY_STATES:
            await self._answer(query, "Кружочек уже обрабатывается. Дождись результата.")
            return

        can_retry, can_manual = video_action_flags(job)
        if action == "retry" and not can_retry:
            await self._answer(query, "Повторные попытки недоступны или закончились.", True)
            await self.refresh_widget(*key)
            return
        if action == "manual" and not can_manual:
            await self._answer(query, "Ручной ввод пока недоступен.", True)
            await self.refresh_widget(*key)
            return

        self._active.add(key)
        try:
            if action == "retry":
                # Условный SQL UPDATE не допускает двойного списания попытки.
                if not self.db.reserve_video_retry(*key):
                    await self._answer(query, "Повтор недоступен.", True)
                    return
                await self._answer(query, "Повторный анализ запущен.")
                await self.refresh_widget(*key)
                await self._process(*key)
            else:
                await self._ask_manual(query, job)
        finally:
            self._active.discard(key)

    async def _ask_manual(self, query: CallbackQuery, job: VideoJob) -> None:
        key = (job.chat_id, job.message_id)
        pending = self.db.conn.execute(
            "SELECT prompt_message_id, invalid_attempts FROM pending_manual WHERE chat_id=? AND video_message_id=?", key,
        ).fetchone()
        if pending and pending[1] >= MAX_INVALID_MANUAL_ATTEMPTS:
            await self._answer(query, "Ручной ввод заблокирован после трёх ошибок. Можно повторить анализ.", True)
            return
        if pending and pending[0] is not None:
            await self._answer(query, "Запрос уже отправлен. Ответь числом на него или на исходный кружочек.", True)
            return
        create_pending_manual(
            self.db, *key, job.user_id, job.display_name,
            datetime.fromisoformat(job.sent_at_utc),
            expires_at=datetime.fromisoformat(job.action_expires_at_utc),
        )
        await self._answer(query)
        until = datetime.fromisoformat(job.action_expires_at_utc).astimezone(self.tz)
        try:
            prompt = await self.bot.send_message(
                chat_id=job.chat_id,
                message_thread_id=job.message_thread_id,
                text=(
                    f"✏️ {html.escape(job.display_name)}, укажи итог для этого кружочка целым неотрицательным числом.\n"
                    f"Ответь на это сообщение до {until:%H:%M} ({self.tz.key}).\n"
                    f"До {MAX_MANUAL_VIDEOS_PER_DAY} ручных кружочков в сутки и {MAX_INVALID_MANUAL_ATTEMPTS} ошибочных попыток. "
                    "Новое число заменит предыдущий результат кружочка."
                ),
                parse_mode="HTML",
                reply_parameters=ReplyParameters(message_id=job.message_id),
                reply_markup=ForceReply(selective=True, input_field_placeholder="Например: 25"),
            )
        except TelegramAPIError:
            log.exception("Не удалось отправить запрос ручного ввода chat=%s msg=%s", *key)
            await self._answer(query, "Не удалось отправить запрос. Попробуй ещё раз.", True)
            return
        with self.db.conn:
            self.db.conn.execute(
                "UPDATE pending_manual SET prompt_message_id=? WHERE chat_id=? AND video_message_id=?",
                (prompt.message_id, *key),
            )

    async def recover(self) -> None:
        for job in self.db.video_jobs_to_recover():
            self.db.complete_video_analysis(
                job.chat_id, job.message_id, count=None,
                error="Обработка прервалась при перезапуске бота.",
            )
            if self.job_allowed(job):
                await self.refresh_widget(job.chat_id, job.message_id)
        # Обновить активные карточки предыдущей версии на Rich HTML после запуска.
        with self.db.conn:
            self.db.conn.execute(
                "UPDATE video_jobs SET widget_dirty=1 "
                "WHERE widget_message_id IS NOT NULL AND action_expires_at_utc>?",
                (datetime.now(timezone.utc).isoformat(),),
            )
        await self.expire_actions()

    async def expire_actions(self) -> None:
        self.db.lock_expired_video_jobs()
        jobs = {
            (job.chat_id, job.message_id): job
            for job in self.db.video_widgets_to_refresh() + self.db.expired_video_widgets()
        }
        for key, job in jobs.items():
            if key in self._active or job.state in BUSY_STATES or not self.job_allowed(job):
                continue
            await self.refresh_widget(*key)
            if job.result_locked or self.expired(job):
                self._widget_locks.pop(key, None)
