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


def processing_bar(frame: int, width: int = 10) -> str:
    """Indeterminate indicator: the model does not expose completion percentages."""
    span = 3
    travel = width - span
    position = frame % (2 * travel)
    start = min(position, 2 * travel - position)
    return "░" * start + "█" * span + "░" * (travel - start)


def render_video_card(
    job: VideoJob,
    result: tuple[int, bool] | None,
    tz: ZoneInfo,
    note: str = "",
    now: datetime | None = None,
    *, progress_frame: int = 0, duplicate_waived: bool = False,
) -> tuple[InputRichMessage, bool]:
    """Вернуть компактную карточку и признак доступности действий."""
    
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now должен содержать timezone")

    if job.state == "duplicate":
        if duplicate_waived:
            return InputRichMessage(html="<p>🚫 Этот кружок уже был прислан. Не засчитано. Штраф на этот день отключён владельцем.</p>"), False
        return InputRichMessage(html=(
            "<p>Этот кружок ты уже присылал, ты хотел всех наебать а наебали тебя, "
            "за такое у тебя х5 норма за сегодня, либо штраф, нехуй наебывать 😘😘😘</p>"
        )), False

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

    expired = bool(job.actions_closed or (deadline is not None and now >= deadline))
    if expired:
        summary = f"💪 Отжимания: <strong>{result[0]}</strong>" if result is not None else "💪 Отжимания: не засчитано"
        return InputRichMessage(html=f"<p>{summary}</p>"), False
    actions_open = terminal
    manual_open = actions_open and job.completed_attempts >= 2 and deadline is not None

    # Компактная карточка с индикатором активности, без фиктивных процентов.
    lines = [
        "<h3>💪 Отжимания</h3>",
        f"<p><strong>{html.escape(job.display_name)}</strong></p>",
        f"<p>{status}</p>",
    ]

    if not terminal:
        bar = "░" * 10 if job.state == "queued" else processing_bar(progress_frame)
        lines.append(f"<p><code>{bar}</code></p>")

    if result is not None:
        count, manual = result
        label = "Результат" if job.state == "done" else "Предыдущий результат"
        lines.append(f"<h3>{label}: {count}</h3>")
        if manual and job.state != "done":
            lines.append("<footer>Указан вручную</footer>")

    if job.state == "failed":
        error = job.error or "Не удалось распознать отжимания."
        lines.append(f"<footer>{html.escape(error)}</footer>")
        if job.completed_attempts < 2:
            lines.append("<footer>Повтори анализ, чтобы открыть ручной ввод.</footer>")

    if note:
        lines.append(f"<footer>{html.escape(note)}</footer>")

    if actions_open:
        lines.append('<tg-button-row align="left">')
        if manual_open:
            label = "Ввести вручную" if result is None else "Изменить"
            lines.append(
                '<tg-button type="callback_data" style="primary" '
                f'data="video:manual:{job.message_id}">✏️ {label}</tg-button>'
            )
        lines.extend([
            '<tg-button type="callback_data" '
            f'data="video:retry:{job.message_id}">🔄 Повторить</tg-button>',
            "</tg-button-row>",
        ])
        if deadline is not None:
            lines.append(f"<footer>Доступно до {deadline.astimezone(tz):%d.%m %H:%M}</footer>")

    return InputRichMessage(html="\n".join(lines)), actions_open
