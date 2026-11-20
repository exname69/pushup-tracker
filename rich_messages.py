"""Rich HTML-карточки и методы Bot API для aiogram 3.24."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram.methods.base import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, Message, ReplyParameters
from aiogram.types.base import TelegramObject
from pydantic import Field

from storage import VideoJob


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

    deadline = (
        datetime.fromisoformat(job.action_expires_at_utc)
        if job.action_expires_at_utc
        else None
    )
    if deadline is not None and deadline.tzinfo is None:
        # В storage поле имеет суффикс _utc: считаем naive-значение временем UTC.
        deadline = deadline.replace(tzinfo=timezone.utc)

    actions_open = bool(terminal and deadline is not None and now < deadline)
    expired = bool(terminal and deadline is not None and now >= deadline)

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

    if actions_open:
        lines.extend(
            [
                '<tg-button-row align="left">',
                (
                    '<tg-button type="callback_data" style="primary" '
                    f'data="video:manual:{job.message_id}">'
                    "✏️ Изменить</tg-button>"
                ),
                (
                    '<tg-button type="callback_data" '
                    f'data="video:retry:{job.message_id}">'
                    "🔄 Повторить</tg-button>"
                ),
                "</tg-button-row>",
                f"<footer>Доступно до {deadline.astimezone(tz):%d.%m %H:%M}</footer>",
            ]
        )
    elif expired:
        lines.append("<footer>🔒 Время изменения результата истекло</footer>")

    return InputRichMessage(html="\n".join(lines)), actions_open
