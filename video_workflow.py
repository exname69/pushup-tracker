"""Rich HTML-карточки групповых кружочков и действия их авторов."""
from __future__ import annotations

import asyncio
import html
import hashlib
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

from storage import Storage, VideoJob
from tracker_core import (
    MAX_INVALID_MANUAL_ATTEMPTS, MAX_MANUAL_VIDEOS_PER_DAY, create_pending_manual,
)
from vision import MAX_VIDEO_BYTES, ModelOverloadedError, analyze_video

log = logging.getLogger(__name__)
BUSY_STATES = {"queued", "downloading", "analyzing"}
PROGRESS_INTERVAL = 1.0
PROCESS_TIMEOUT_SECONDS = 240
WIDGET_TIMEOUT_SECONDS = 10


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

    @staticmethod
    def expired(job: VideoJob) -> bool:
        return bool(
            job.actions_closed or (
                job.action_expires_at_utc
                and datetime.now(timezone.utc) >= datetime.fromisoformat(job.action_expires_at_utc)
            )
        )

    async def refresh_widget(
        self, chat_id: int, message_id: int, note: str = "",
        *, progress_frame: int | None = None,
    ) -> float | None:
        """Bound Telegram edits and lock waits so UI failures cannot stall processing."""
        try:
            async with asyncio.timeout(WIDGET_TIMEOUT_SECONDS):
                return await self._refresh_widget(chat_id, message_id, note, progress_frame=progress_frame)
        except TimeoutError:
            log.warning("Таймаут обновления карточки chat=%s msg=%s", chat_id, message_id)
            return float(WIDGET_TIMEOUT_SECONDS)

    async def _refresh_widget(
        self, chat_id: int, message_id: int, note: str = "",
        *, progress_frame: int | None = None,
    ) -> float | None:
        key = (chat_id, message_id)
        lock = self._widget_locks.setdefault(key, asyncio.Lock())
        async with lock:
            for attempt in range(3):
                job = self.db.get_video_job(*key)
                if job is None:
                    return
                # An animation tick must never overwrite a terminal card.
                if progress_frame is not None and (
                    job.state not in {"downloading", "analyzing"} or job.widget_message_id is None
                ):
                    return
                result = self.db.get_video_result(*key)
                rendered_at = datetime.now(timezone.utc)
                locked = bool(job.actions_closed or (
                    job.action_expires_at_utc
                    and rendered_at >= datetime.fromisoformat(job.action_expires_at_utc)
                ))
                content, actions_open = render_video_card(
                    job, result, self.tz, note, now=rendered_at, progress_frame=progress_frame or 0,
                )
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
                    self._mark_rendered(job, result, actions_open, locked)
                    return
                except TelegramRetryAfter as exc:
                    if progress_frame is not None:
                        # Back off outside the widget lock; do not delay the result.
                        return float(exc.retry_after)
                    if attempt < 2 and exc.retry_after < WIDGET_TIMEOUT_SECONDS / 2:
                        await asyncio.sleep(exc.retry_after)
                        continue
                    log.warning("Не удалось обновить карточку: flood control chat=%s msg=%s", *key)
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc).lower():
                        self._mark_rendered(job, result, actions_open, locked)
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

    def _mark_rendered(self, job: VideoJob, result, actions_open: bool, locked: bool) -> None:
        key = (job.chat_id, job.message_id)
        current = self.db.get_video_job(*key)
        if current is None:
            return
        # Ручной ответ мог изменить результат, пока Telegram принимал editMessageText.
        if (current.state, current.error, current.action_expires_at_utc,
            current.completed_attempts, self.db.get_video_result(*key)) != (
            job.state, job.error, job.action_expires_at_utc, job.completed_attempts, result,
        ):
            return
        self.db.mark_video_widget_clean(*key)
        if locked and not actions_open:
            self.db.mark_video_actions_closed(*key)

    async def on_video(self, message: Message) -> None:
        if not message.from_user or message.from_user.is_bot or not message.video_note:
            return
        key = (message.chat.id, message.message_id)
        if self.db.has_video(*key) or self.db.get_video_job(*key):
            return
        note = message.video_note
        self.db.register_chat(message.chat.id, message.chat.title or "Группа")
        created = self.db.create_video_job(
            *key, message.from_user.id, message.from_user.full_name,
            note.file_id, note.duration, note.file_size, message.date,
            message_thread_id=message.message_thread_id,
        )
        if not created:
            return
        duplicate = self.db.check_video_fingerprints(
            *key, {"file_id": note.file_id, "file_unique_id": note.file_unique_id}, self.tz,
        )
        if duplicate:
            await self.refresh_widget(*key)
            self._widget_locks.pop(key, None)
            return
        self._active.add(key)
        try:
            await self.refresh_widget(*key)
            await self._process(*key)
        finally:
            self._active.discard(key)

    async def _animate_widget(self, key: tuple[int, int], progress: dict[str, str]) -> None:
        frame = 0
        delay = PROGRESS_INTERVAL
        while True:
            await asyncio.sleep(delay)
            job = self.db.get_video_job(*key)
            if job is None or job.state not in BUSY_STATES or self.expired(job):
                return
            if job.state == "queued":
                continue
            frame += 1
            try:
                retry_after = await self.refresh_widget(
                    *key, note=progress.get("note", ""), progress_frame=frame,
                )
                delay = max(PROGRESS_INTERVAL, retry_after or 0)
            except Exception:
                # UI errors should not interrupt the actual analysis.
                log.exception("Ошибка анимации карточки chat=%s msg=%s", *key)
                delay = PROGRESS_INTERVAL

    async def _process(self, chat_id: int, message_id: int) -> None:
        key = (chat_id, message_id)
        progress: dict[str, str] = {}
        animation = asyncio.create_task(self._animate_widget(key, progress))
        try:
            async with asyncio.timeout(PROCESS_TIMEOUT_SECONDS):
                await self._run_processing(*key, progress=progress)
        except TimeoutError:
            log.warning("Общий таймаут обработки chat=%s msg=%s", *key)
            self._fail_stalled_job(key, "Время обработки истекло. Попробуй повторить анализ.")
        except Exception:
            log.exception("Необработанная ошибка кружочка chat=%s msg=%s", *key)
            self._fail_stalled_job(key, "Не удалось завершить обработку.")
        finally:
            animation.cancel()
            await asyncio.gather(animation, return_exceptions=True)
        await self.refresh_widget(*key)

    def _fail_stalled_job(self, key: tuple[int, int], error: str, *, interrupted: bool = False) -> None:
        job = self.db.get_video_job(*key)
        if job is None or job.state not in BUSY_STATES:
            return
        if self.expired(job):
            previous = self.db.get_video_result(*key)
            self.db.finish_video_job(*key, "done" if previous is not None else "failed", attempt_completed=False)
        else:
            self.db.finish_video_job(
                *key, "failed", error=error,
                attempt_completed=not interrupted and job.state != "queued",
            )

    async def _run_processing(
        self, chat_id: int, message_id: int, *, progress: dict[str, str],
    ) -> None:
        key = (chat_id, message_id)
        job = self.db.get_video_job(*key)
        if job is None:
            return
        analysis = None
        error = "Не удалось уверенно распознать отжимания."
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
                        if target.stat().st_size > 0:
                            def digest() -> str:
                                with target.open("rb") as video:
                                    return hashlib.file_digest(video, "sha256").hexdigest()

                            checksum = await asyncio.to_thread(digest)
                            if self.db.check_video_fingerprints(
                                *key, {"sha256": checksum}, self.tz,
                                preserve_existing=self.db.has_video(*key),
                            ):
                                return
                        self.db.set_video_job_state(*key, "analyzing")
                        await self.refresh_widget(*key)
                        try:
                            analysis = await analyze_video(target, duration_seconds=job.duration)
                        except ModelOverloadedError:
                            primary = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
                            if primary != self.fallback_model:
                                progress["note"] = "Основная модель недоступна или не ответила вовремя. Пробую резервную…"
                                await self.refresh_widget(*key, note=progress["note"])
                                try:
                                    analysis = await analyze_video(
                                        target, duration_seconds=job.duration, model_override=self.fallback_model,
                                    )
                                except ModelOverloadedError:
                                    log.warning("Резервная модель недоступна или не ответила chat=%s msg=%s", *key)
                                    error = "Обе модели Gemini временно недоступны или не ответили вовремя."
                            else:
                                error = "Модель Gemini временно недоступна или не ответила вовремя."
        except Exception:
            log.exception("Ошибка обработки кружочка chat=%s msg=%s", *key)
            error = "Не удалось обработать видео."
        # A retry finishing after the fixed deadline must not change the count.
        if self.expired(job):
            previous = self.db.get_video_result(*key)
            self.db.finish_video_job(
                *key, "done" if previous is not None else "failed", attempt_completed=False,
            )
            return
        if analysis is not None and not analysis.uncertain:
            self.db.replace_video_result(*key, analysis.repetitions)
            self.db.finish_video_job(*key, "done")
        else:
            self.db.finish_video_job(*key, "failed", error=error)

    @staticmethod
    async def _answer(query: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
        try:
            async with asyncio.timeout(WIDGET_TIMEOUT_SECONDS):
                await query.answer(text, show_alert=alert)
        except (TelegramAPIError, TimeoutError):
            log.warning("Не удалось подтвердить нажатие кнопки", exc_info=True)

    async def on_callback(self, query: CallbackQuery) -> None:
        match = re.fullmatch(r"video:(manual|retry):([0-9]{1,20})", query.data or "")
        if match is None or not isinstance(query.message, Message):
            await self._answer(query, "Карточка недоступна.", True)
            return
        chat_id = query.message.chat.id
        if query.message.chat.type not in ("group", "supergroup") or not self.access.allowed("group", chat_id):
            await self._answer(query, "В этой группе действие недоступно.", True)
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
        if self.expired(job):
            await self._answer(query, "Время изменения истекло. Результат заблокирован.", True)
            await self.refresh_widget(*key)
            return
        if key in self._active or job.state not in ("done", "failed"):
            await self._answer(query, "Этот кружочек уже обрабатывается. Дождись результата.")
            return
        if action == "manual" and (job.completed_attempts < 2 or not job.action_expires_at_utc):
            await self._answer(query, "Сначала повтори анализ кружочка и дождись результата.", True)
            return
        self._active.add(key)
        try:
            if action == "retry":
                # Резервируем кружочек до первого await: двойной клик не запустит два анализа.
                self.db.set_video_job_state(*key, "queued")
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
            "SELECT prompt_message_id, invalid_attempts, expires_at_utc FROM pending_manual WHERE chat_id=? AND video_message_id=?", key,
        ).fetchone()
        if pending and pending[1] >= MAX_INVALID_MANUAL_ATTEMPTS:
            await self._answer(query, "Ручной ввод заблокирован после трёх ошибок. Можно повторить анализ.", True)
            return
        if pending and pending[2] != job.action_expires_at_utc:
            # A legacy manual prompt may predate the new retry gate.
            with self.db.conn:
                self.db.conn.execute(
                    "UPDATE pending_manual SET expires_at_utc=?, prompt_message_id=NULL "
                    "WHERE chat_id=? AND video_message_id=?",
                    (job.action_expires_at_utc, *key),
                )
            pending = None
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
            self.db.finish_video_job(
                job.chat_id, job.message_id, "failed",
                error="Обработка прервалась при перезапуске бота. Можно запустить её заново.",
                attempt_completed=False,
            )
            if self.access.allowed("group", job.chat_id):
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
        # An interrupted handler can leave a busy job without an active task.
        # Recover it even when the process itself has not restarted.
        for job in self.db.video_jobs_to_recover():
            key = (job.chat_id, job.message_id)
            if key not in self._active:
                self._fail_stalled_job(
                    key, "Обработка прервалась. Можно повторить анализ.", interrupted=True,
                )
        jobs = {
            (job.chat_id, job.message_id): job
            for job in self.db.video_widgets_to_refresh() + self.db.expired_video_widgets()
        }
        for key, job in jobs.items():
            if not self.access.allowed("group", job.chat_id):
                continue
            busy = key in self._active or job.state in BUSY_STATES
            if busy and not self.expired(job):
                continue
            await self.refresh_widget(*key)
            if not busy and self.expired(job):
                self._widget_locks.pop(key, None)
