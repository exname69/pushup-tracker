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
    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

    prompt = (
        "Analyze the provided video and accurately count the number of completed push-ups. "
        "A push-up counts as completed only when the person lowers their body and then pushes "
        "back up to the starting position. Count each full repetition exactly once. "
        "Technique may be imperfect, legs may be outside the frame, and the camera angle "
        "may be frontal. Do not require perfect form, but ignore incomplete repetitions, "
        "head movements, talking, camera shaking, and repetitions that are not visible. "
        "Record the timestamp (in seconds from the beginning of the video) when each "
        "repetition is completed. "
        "Count only observable movement cycles. Never estimate repetitions based on "
        "assumed exercise pace. "
        "Return strictly a valid JSON object with this exact structure: "
        '{"count": 3, "completed_at_seconds": [2.4, 4.1, 6.3], "uncertain": false}. '
        "Requirements: "
        "count must exactly match the length of completed_at_seconds. "
        "Timestamps must be numeric, in seconds, and sorted chronologically. "
        'If there are definitely no completed push-ups, return '
        '{"count": 0, "completed_at_seconds": [], "uncertain": false}. '
        "Set uncertain to true if the video quality, visibility, or ambiguous movement "
        "cycles prevent a reliable count. "
        "Do not invent repetitions or timestamps. "
        "Output only the raw JSON object. No Markdown, code fences, explanations, "
        "or additional text."
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
