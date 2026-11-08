"""Подсчёт отжиманий через Gemini Video API.

Для использования нужен GEMINI_API_KEY в окружении контейнера.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Analysis:
    repetitions: int
    valid_frames: int
    sampled_frames: int


def analyze_video(path: str | Path, target_fps: int = 5) -> Analysis | None:
    """Попросить Gemini посчитать циклы движения в видео.

    None означает, что результат недостоверен или запрос не удался.
    valid_frames/sampled_frames оставлены ради совместимости с bot.py:
    это НЕ диагностические метрики pose-детектора.
    """
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        log.error("GEMINI_API_KEY не указан")
        return None

    video_path = Path(path)
    if not video_path.is_file() or video_path.stat().st_size == 0:
        return None
    if video_path.stat().st_size > 20 * 1024 * 1024:
        log.warning("Видео слишком большое для текущей конфигурации")
        return None

    fps = max(2, min(int(target_fps), 8))
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    prompt = (
        "Ты анализируешь ОДНО короткое видео с отжиманиями. "
        "Посчитай только реально видимые законченные циклы опускания и подъёма корпуса. "
        "Техника может быть плохой, ноги могут быть вне кадра, ракурс может быть фронтальным. "
        "Не требуй идеальной техники, но не считай разговор, движения головой, дрожание камеры, "
        "неполные движения и невидимые повторы. "
        "Определи моменты каждого завершённого подъёма. "
        "Если циклы неотличимы или качество недостаточно, укажи uncertain=true. "
        "Не оценивай результат по предполагаемому темпу; считай по наблюдаемым циклам. "
        "Верни строго JSON-объект вида: "
        '{"count": 3, "completed_at_seconds": [2.4, 4.1, 6.3], "uncertain": false}. '
        "count должен в точности совпадать с длиной completed_at_seconds. "
        "Если отжиманий точно нет, верни count=0 и пустой список."
    )

    try:
        with genai.Client(api_key=api_key) as client:
            response = client.models.generate_content(
                model=model,
                contents=types.Content(
                    parts=[
                        types.Part(
                            inline_data=types.Blob(
                                data=video_path.read_bytes(),
                                mime_type="video/mp4",
                            ),
                            video_metadata=types.VideoMetadata(fps=fps),
                        ),
                        types.Part(text=prompt),
                    ]
                ),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0,
                ),
            )
        result = json.loads(response.text or "{}")
        count = result.get("count")
        stamps = result.get("completed_at_seconds")
        uncertain = result.get("uncertain")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0 or count > 1000:
            return None
        if not isinstance(stamps, list) or len(stamps) != count or uncertain is not False:
            return None
        if any(isinstance(t, bool) or not isinstance(t, (int, float)) or t < 0 for t in stamps):
            return None
        if any(b <= a for a, b in zip(stamps, stamps[1:])):
            return None
        log.info("Gemini анализ: повторений=%d, метки=%s", count, stamps)
        # Значения оставлены совместимыми с существующим bot.py.
        # Они НЕ являются числом кадров с обнаруженной позой.
        return Analysis(repetitions=count, valid_frames=1, sampled_frames=1)
    except Exception:
        log.exception("Gemini: ошибка анализа видео")
        return None
