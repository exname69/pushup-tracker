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
    target_fps: int = 5,
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

    fps = max(2, min(int(target_fps), 8))
    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

    prompt = (
        "Analyze the provided video and count ALL observable push-up-like repetitions. "
        "Your primary goal is to count every distinct up-and-down movement cycle, "
        "not to evaluate exercise technique or form. "
        "IMPORTANT: Use very lenient counting criteria. "
        "Count ALL repetitions, including partial push-ups, shallow movements, "
        "short-range repetitions, fast repetitions, poorly executed push-ups, "
        "and rhythmic body rocking that resembles push-up motion. "
        "A full range of motion is NOT required. "
        "The arms do NOT need to fully straighten or bend. "
        "The chest does NOT need to approach or touch the floor. "
        "The body does NOT need to remain perfectly straight. "
        "Treat each distinct downward-and-upward movement of the torso, "
        "shoulders, or upper body as one repetition, even when the movement "
        "is small or the technique is incorrect. "
        "Pay special attention to consecutive, repetitive movements. "
        "DO NOT skip repetitions simply because they look nearly identical. "
        "DO NOT merge multiple short or fast repetitions into a single repetition. "
        "Count each clearly distinguishable movement cycle independently. "
        "The camera may be frontal, the legs may be outside the frame, "
        "and parts of the body may be obscured. "
        "Use visible torso, shoulder, and arm movement to identify repetitions. "
        "Do not require the entire body to be visible. "
        "Analyze the entire video from beginning to end. "
        "Track the repeating motion continuously and identify each upward "
        "phase following a downward phase. "
        "Record the timestamp in seconds when each upward movement is completed. "
        "Do not count unrelated head movements, talking, or camera shaking "
        "unless there is observable corresponding rhythmic upper-body movement. "
        "Do not invent movements that are not visible. "
        "Do not estimate repetitions from assumed exercise speed. "
        "When a movement is visibly part of the repeated push-up-like motion, "
        "prefer counting it rather than rejecting it due to imperfect technique. "
        "Use uncertain=true only when the video is genuinely too ambiguous "
        "to distinguish individual movement cycles. "
        "Do not set uncertain=true merely because the technique is poor "
        "or the range of motion is small. "
        "Return strictly a valid JSON object with this exact structure: "
        '{"count": 3, "completed_at_seconds": [2.4, 4.1, 6.3], "uncertain": false}. '
        "Requirements: "
        "count must exactly equal the length of completed_at_seconds. "
        "Timestamps must be numeric, in seconds, and sorted chronologically. "
        "If there are definitely no observable push-up-like movements, return "
        '{"count": 0, "completed_at_seconds": [], "uncertain": false}. '
        "Do not invent repetitions or timestamps. "
        "Output only the raw JSON object. "
        "No Markdown, code fences, explanations, or additional text."
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
