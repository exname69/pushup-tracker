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
    """Метод sendRichMessage, отсутствующий в закреплённой версии aiogram."""

    __api_method__ = "sendRichMessage"
    __returning__ = Message

    chat_id: int
    rich_message: InputRichMessage
    message_thread_id: int | None = None
    reply_parameters: ReplyParameters | None = None


class EditRichMessage(TelegramMethod[Message | bool]):
    """Обновить rich_message и убрать клавиатуру прежней текстовой карточки."""

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
    """Вернуть Rich HTML и признак доступности действий в этой версии карточки."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now должен содержать timezone")
    states = {
        "queued": ("⏳ В очереди", ("Ожидает", "Ожидает", "Ожидает")),
        "downloading": ("📥 Скачиваю видео", ("▶ Выполняется", "Ожидает", "Ожидает")),
        "analyzing": ("🔎 Считаю отжимания", ("✓ Готово", "▶ Выполняется", "Ожидает")),
        "done": ("✅ Обработка завершена", ("✓ Готово", "✓ Готово", "✓ Сохранён")),
        "failed": ("⚠️ Не удалось завершить обработку", ("—", "—", "Не получен")),
    }
    status, steps = states[job.state]
    completed_segments = {"queued": 0, "downloading": 1, "analyzing": 2, "done": 3, "failed": 3}[job.state]
    progress_bar = "████" * completed_segments + "░░░░" * (3 - completed_segments)
    # Ручной результат может быть сохранён без успешного скачивания или анализа.
    if job.state == "done" and result is not None and result[1]:
        status = "✍️ Результат обновлён вручную"
        steps = ("—", "—", "✓ Сохранён")
    deadline = (
        datetime.fromisoformat(job.action_expires_at_utc)
        if job.action_expires_at_utc else None
    )
    terminal = job.state in ("done", "failed")
    actions_open = bool(terminal and deadline is not None and now < deadline)
    expired = bool(deadline is not None and now >= deadline)
    lines = [
        "<h3>💪 Отжимания</h3>",
        f"<p><strong>{html.escape(job.display_name)}</strong></p>",
        f"<p><mark>{status}</mark></p>",
        f"<p><code>{progress_bar}</code></p>",
    ]
    if result is not None:
        count, manual = result
        label = "Результат" if job.state == "done" else "Сохранённый результат"
        origin = "Указан вручную" if manual else "Автоматический подсчёт"
        lines += [
            f"<h3>{label}: {count} повторов</h3>",
            f"<p>{origin}</p>",
        ]
    elif terminal:
        lines.append("<p>Результат пока не записан.</p>")
    lines += [
        '<table bordered striped compact><tr><th>Этап</th><th>Состояние</th></tr>',
        *(
            f"<tr><td>{stage}</td><td>{step}</td></tr>"
            for stage, step in zip(("Скачивание", "Анализ", "Результат"), steps)
        ),
        "</table>",
    ]
    if note:
        lines.append(f"<p>{html.escape(note)}</p>")
    if job.state == "failed":
        lines.append(
            f"<p>{html.escape(job.error or 'Не удалось уверенно распознать отжимания.')}</p>"
        )
        if result is not None:
            lines.append("<p>Предыдущее число продолжает учитываться в статистике.</p>")
    if actions_open:
        until = deadline.astimezone(tz)
        lines += [
            "<p>Автор может изменить результат или запустить повторный анализ.</p>",
            '<tg-button-row align="left">',
            f'<tg-button type="callback_data" style="primary" data="video:manual:{job.message_id}">'
            "Изменить результат</tg-button>",
            f'<tg-button type="callback_data" data="video:retry:{job.message_id}">'
            "Обработать заново</tg-button>",
            "</tg-button-row>",
            f"<footer>Доступно до {until:%d.%m.%Y %H:%M:%S} ({html.escape(tz.key)}).</footer>",
        ]
    elif terminal and expired:
        lines.append("<footer>🔒 30 минут истекли. Для исправления обратись к владельцу.</footer>")
    elif not terminal:
        lines.append("<footer>Карточка обновится после завершения этапа.</footer>")
    sent_at = datetime.fromisoformat(job.sent_at_utc).astimezone(tz)
    lines += [
        "<details><summary>О кружочке и изменении результата</summary>",
        f"<p>Отправлен {sent_at:%d.%m.%Y %H:%M:%S} ({html.escape(tz.key)}). "
        f"Длительность: {job.duration} с.</p>",
        "<p>Менять число и повторять анализ может только автор в течение 30 минут "
        "с первого результата или ошибки. Повторные действия не продлевают срок.</p>",
        "<p>Ручная правка заменяет число для этого кружочка. Если повторный анализ "
        "не удался, предыдущий результат сохраняется.</p>",
        "</details>",
    ]
    return InputRichMessage(html="\n".join(lines)), actions_open
