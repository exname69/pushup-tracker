"""Подсчёт отжиманий через Gemini Video API.

Нужен GEMINI_API_KEY. Модель и параметры можно задать через окружение:
GEMINI_MODEL,.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)
MAX_VIDEO_BYTES = 20 * 1024 * 1024
MAX_REPETITIONS = 1000


@dataclass(frozen=True)
class Analysis:
    repetitions: int
    completed_at_seconds: tuple[float, ...] = ()
    uncertain: bool = False
    usage_metadata: dict[str, int] | None = None
    model: str | None = None
    finish_reason: str | None = None
    requested_fps: int | None = None
    api_seconds: float | None = None


def analyze_video(
    path: str | Path,
    target_fps: int = 8,
    *,
    duration_seconds: float | None = None,
) -> Analysis | None:
    """Подсчитать видимые циклы движения.

    None — ошибка/невалидный ответ. Анализ с uncertain=True возвращается
    для диагностики, но НЕ должен записываться в статистику группы.
    """
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        log.error("GEMINI_API_KEY не указан")
        return None

    video_path = Path(path)
    if not video_path.is_file():
        log.warning("Видео не найдено: %s", video_path)
        return None
    size = video_path.stat().st_size
    if size <= 0 or size > MAX_VIDEO_BYTES:
        log.warning("Неверный размер видео: %d байт", size)
        return None

    fps = max(4, min(int(target_fps), 10))
    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()


    prompt = (
        "Count ALL visible push-up-like movements in the video. "
        "Count every distinct down-up body movement as one repetition, "
        "including partial, shallow, fast, sloppy push-ups and rhythmic body rocking. "
        "Accept ANY camera angle, especially frontal views. "
        "Do NOT require proper technique, full arm extension, visible legs, "
        "or the entire body to be in frame. "
        "Track torso, shoulder, and arm movements. "
        "Do NOT skip small, fast, or nearly identical repetitions. "
        "Count every observable cycle exactly once, even with minimal movement. "
        "Ignore unrelated movements and camera shake. "
        "Never invent invisible repetitions or estimate by rhythm alone. "
        "Record the completion time of each repetition in seconds. "
        "Set uncertain=true only if individual movements cannot be distinguished. "
        "Return ONLY valid JSON: "
        '{"count": 3, "completed_at_seconds": [2.4, 4.1, 6.3], "uncertain": false}. '
        "count MUST equal the number of timestamps, sorted chronologically. "
        'If no repetitions are visible, return '
        '{"count": 0, "completed_at_seconds": [], "uncertain": false}.'
    )


    response_schema = {
        "type": "OBJECT",
        "properties": {
            "count": {"type": "INTEGER"},
            "completed_at_seconds": {
                "type": "ARRAY",
                "items": {"type": "NUMBER"},
            },
            "uncertain": {"type": "BOOLEAN"},
        },
        "required": ["count", "completed_at_seconds", "uncertain"],
    }

    try:
        started = time.perf_counter()
        with genai.Client(api_key=api_key) as client:
            response = client.models.generate_content(
                model=model,
                contents=types.Content(
                    parts=[
                        types.Part(
                            inline_data=types.Blob(
                                data=video_path.read_bytes(), mime_type="video/mp4"
                            ),
                            video_metadata=types.VideoMetadata(fps=fps),
                        ),
                        types.Part(text=prompt),
                    ]
                ),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=response_schema,
                    temperature=0,
                ),
            )
        api_seconds = time.perf_counter() - started

        usage = getattr(response, "usage_metadata", None)
        usage_dict = None
        if usage is not None:
            usage_dict = {
                key: value
                for key in (
                    "prompt_token_count",
                    "candidates_token_count",
                    "thoughts_token_count",
                    "cached_content_token_count",
                    "total_token_count",
                )
                if isinstance((value := getattr(usage, key, None)), int)
            }

        candidates = getattr(response, "candidates", None) or []
        finish = getattr(candidates[0], "finish_reason", None) if candidates else None
        finish_reason = str(getattr(finish, "value", finish)) if finish is not None else None

        try:
            result = json.loads(response.text or "")
        except (TypeError, ValueError):
            log.warning(
                "Gemini вернул невалидный JSON: finish=%s usage=%s elapsed=%.2fs",
                finish_reason, usage_dict, api_seconds,
            )
            return None

        if not isinstance(result, dict):
            log.warning("Gemini вернул JSON, но не объект")
            return None

        count = result.get("count")
        stamps = result.get("completed_at_seconds")
        uncertain = result.get("uncertain")
        if type(count) is not int or not 0 <= count <= MAX_REPETITIONS:
            log.warning("Gemini: недопустимый count=%r", count)
            return None
        if type(uncertain) is not bool:
            log.warning("Gemini: отсутствует корректный uncertain=%r", uncertain)
            return None
        if not isinstance(stamps, list) or len(stamps) != count:
            log.warning("Gemini: count=%d не совпадает с длиной меток", count)
            return None

        timestamps: list[float] = []
        for timestamp in stamps:
            if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0:
                log.warning("Gemini: недопустимая временная метка=%r", timestamp)
                return None
            t = float(timestamp)
            if timestamps and t <= timestamps[-1]:
                log.warning("Gemini: метки не отсортированы или повторяются")
                return None
            if duration_seconds is not None and t > duration_seconds + 1.0:
                log.warning("Gemini: метка %.2f за пределами видео %.2fs", t, duration_seconds)
                return None
            timestamps.append(t)

        log.info(
            "Gemini: model=%s count=%d uncertain=%s fps=%d api=%.2fs finish=%s usage=%s timestamps=%s",
            model, count, uncertain, fps, api_seconds, finish_reason, usage_dict, timestamps,
        )
        return Analysis(
            repetitions=count,
            completed_at_seconds=tuple(timestamps),
            uncertain=uncertain,
            usage_metadata=usage_dict,
            model=getattr(response, "model_version", None) or model,
            finish_reason=finish_reason,
            requested_fps=fps,
            api_seconds=api_seconds,
        )
    except Exception:
        log.exception("Gemini: ошибка анализа видео")
        return None
