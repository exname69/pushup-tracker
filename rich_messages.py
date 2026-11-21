"""Rich HTML-карточки и методы Bot API для aiogram 3.24."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram.methods.base import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, Message, ReplyParameters
from aiogram.types.base import TelegramObject
from pydantic import Field

from storage import VideoJob, video_action_flags


class InputRichMessage(TelegramObject):
    html: str
    skip_entity_detection: bool = True


class SendRichMessage(TelegramMethod[Message]):
    """Отправка rich-сообщения через метод sendRichMessage."""

    __api_method__ = "sendRichMessage"
    __returning__ = Message

    chat_id: int
    rich_message: InputRichMessage
    message_thread_id: int | None = None
    reply_parameters: ReplyParameters | None = None


class EditRichMessage(TelegramMethod[Message | bool]):
    """Обновление rich-сообщения с удалением старой inline-клавиатуры."""

    __api_method__ = "editMessageText"
    __returning__ = Message | bool

    chat_id: int
    message_id: int
    rich_message: InputRichMessage
    reply_markup: InlineKeyboardMarkup = Field(
        default_factory=lambda: InlineKeyboardMarkup(inline_keyboard=[])
    )


def render_video_card(
    job: VideoJob,
    result: tuple[int, bool] | None,
    tz: ZoneInfo,
    note: str = "",
    now: datetime | None = None,
) -> tuple[InputRichMessage, bool]:
    """Вернуть компактную карточку и признак доступности действий."""
    
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now должен содержать timezone")

    statuses = {
        "queued": "⏳ В очереди",
        "downloading": "📥 Скачиваю видео…",
        "analyzing": "🔍 Считаю отжимания…",
        "done": "✅ Готово",
        "failed": "⚠️ Ошибка обработки",
    }
    status = statuses[job.state]
    terminal = job.state in ("done", "failed")

    # После ручной правки показываем актуальный статус.
    if job.state == "done" and result is not None and result[1]:
        status = "✍️ Результат изменён"

    can_retry, can_manual = video_action_flags(job, now)
    actions_open = can_retry or can_manual

    deadline = (
        datetime.fromisoformat(job.action_expires_at_utc)
        if job.action_expires_at_utc else None
    )
    expired = terminal and deadline is not None and now >= deadline

    # Только заголовок, имя и текущий статус — без таблиц и прогресс-баров.
    lines = [
        "<h3>💪 Отжимания</h3>",
        f"<p><strong>{html.escape(job.display_name)}</strong></p>",
        f"<p>{status}</p>",
    ]

    if result is not None:
        count, manual = result
        label = "Результат" if job.state == "done" else "Предыдущий результат"
        lines.append(f"<h3>{label}: {count}</h3>")
        if manual and job.state != "done":
            lines.append("<footer>Указан вручную</footer>")

    if job.state == "failed":
        error = job.error or "Не удалось распознать отжимания."
        lines.append(f"<footer>{html.escape(error)}</footer>")

    if note:
        lines.append(f"<footer>{html.escape(note)}</footer>")

    if can_retry:
        remaining = job.retry_limit - job.retry_used
        lines.extend([
            '<tg-button-row align="left">',
            (
                '<tg-button type="callback_data" style="primary" '
                f'data="video:retry:{job.message_id}">'
                '🔄 Повторить</tg-button>'
            ),
            '</tg-button-row>',
            f'<footer>Осталось повторов: {remaining}</footer>',
        ])
        if job.result_source == "strong" and job.retry_used == 0 and deadline:
            lines.append(
                f"<footer>🔒 Фиксация через 5 минут: {deadline.astimezone(tz):%H:%M}</footer>"
            )
    elif can_manual:
        lines.extend([
            '<tg-button-row align="left">',
            (
                '<tg-button type="callback_data" style="primary" '
                f'data="video:manual:{job.message_id}">'
                '✏️ Ввести вручную</tg-button>'
            ),
            '</tg-button-row>',
            '<footer>Попытки анализа закончились</footer>',
        ])
        if deadline:
            lines.append(f"<footer>До {deadline.astimezone(tz):%d.%m %H:%M}</footer>")
    elif terminal:
        lines.append("<footer>🔒 Результат зафиксирован</footer>")

    return InputRichMessage(html="\n".join(lines)), actions_open
