"""Подсчёт отжиманий на видео с фронтального, бокового и диагонального ракурса.

Для анализа достаточно видимых плеч и рук; ноги не обязательны.
Счётчик ориентирован на циклы движения, а не на идеальную технику.
При недостаточно надёжном видео возвращает None (для ручного ввода ботом).
"""

from __future__ import annotations

from dataclasses import dataclass
from math import acos, degrees, hypot, isfinite, sqrt
from pathlib import Path
from statistics import median


@dataclass(frozen=True)
class Analysis:
    repetitions: int
    valid_frames: int
    sampled_frames: int


class PushupCounter:
    """Совместимый с прежним кодом счётчик циклов по углу локтя."""

    def __init__(self, down_angle: float = 105.0, up_angle: float = 155.0):
        self.down_angle = down_angle
        self.up_angle = up_angle
        self.state = "unknown"
        self.repetitions = 0
        self._up_streak = 0
        self._down_streak = 0
        self._missing = 0

    def update(self, elbow_angle: float | None) -> None:
        if elbow_angle is None:
            self._missing += 1
            self._up_streak = self._down_streak = 0
            if self._missing >= 8:
                self.state = "unknown"
            return
        self._missing = 0
        self._up_streak = self._up_streak + 1 if elbow_angle >= self.up_angle else 0
        self._down_streak = self._down_streak + 1 if elbow_angle <= self.down_angle else 0
        if self.state == "unknown" and self._up_streak >= 2:
            self.state = "up"
        elif self.state == "up" and self._down_streak >= 2:
            self.state = "down"
        elif self.state == "down" and self._up_streak >= 2:
            self.state = "up"
            self.repetitions += 1


def joint_angle(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    """Угол ABC в градусах, вершина — точка B."""
    ba = (a[0] - b[0], a[1] - b[1])
    bc = (c[0] - b[0], c[1] - b[1])
    norm = hypot(*ba) * hypot(*bc)
    if norm < 1e-8:
        return 0.0
    cos_value = (ba[0] * bc[0] + ba[1] * bc[1]) / norm
    return degrees(acos(max(-1.0, min(1.0, cos_value))))


def _angle_3d(a, b, c) -> float | None:
    ab = (a.x - b.x, a.y - b.y, a.z - b.z)
    cb = (c.x - b.x, c.y - b.y, c.z - b.z)
    norm_a = sqrt(sum(v * v for v in ab))
    norm_c = sqrt(sum(v * v for v in cb))
    if norm_a < 1e-6 or norm_c < 1e-6:
        return None
    cos_value = sum(x * y for x, y in zip(ab, cb)) / (norm_a * norm_c)
    return degrees(acos(max(-1.0, min(1.0, cos_value))))


def _extract_signals(pose_landmarks, pose_world_landmarks, indices) -> dict[str, float]:
    """Извлекает альтернативные признаки «вниз», не требуя видимых ног.

    - elbow_3d: сгибание локтя в 3D (лучше для фронтальной съёмки);
    - elbow_2d: сгибание локтя в плоскости изображения;
    - body: приближение плеч к кистям относительно масштаба плеч.

    У разных ракурсов надёжными оказываются разные признаки.
    """
    landmarks = pose_landmarks.landmark
    world = pose_world_landmarks.landmark if pose_world_landmarks is not None else None
    signals: dict[str, list[float]] = {"elbow_3d": [], "elbow_2d": [], "body": []}
    shoulders = []

    for side in ("LEFT", "RIGHT"):
        sh = landmarks[indices[f"{side}_SHOULDER"].value]
        el = landmarks[indices[f"{side}_ELBOW"].value]
        wr = landmarks[indices[f"{side}_WRIST"].value]
        if sh.visibility >= 0.45:
            shoulders.append(sh)
        if min(sh.visibility, el.visibility, wr.visibility) < 0.45:
            continue

        angle_2d = joint_angle((sh.x, sh.y), (el.x, el.y), (wr.x, wr.y))
        signals["elbow_2d"].append(180.0 - angle_2d)

        if world is not None:
            ws = world[indices[f"{side}_SHOULDER"].value]
            we = world[indices[f"{side}_ELBOW"].value]
            ww = world[indices[f"{side}_WRIST"].value]
            angle_3d = _angle_3d(ws, we, ww)
            if angle_3d is not None:
                signals["elbow_3d"].append(180.0 - angle_3d)

        signals["body"].append(hypot(sh.x - wr.x, sh.y - wr.y))

    output: dict[str, float] = {}
    for key in ("elbow_2d", "elbow_3d"):
        if signals[key]:
            output[key] = median(signals[key])

    # Нормализуем расстояние плечо–кисть по ширине плеч. Если одно плечо
    # закрыто, берём только угловые признаки: масштаб иначе ненадёжен.
    if len(shoulders) == 2 and signals["body"]:
        width = hypot(shoulders[0].x - shoulders[1].x, shoulders[0].y - shoulders[1].y)
        if width > 0.035:
            output["body"] = -median(signals["body"]) / width
    return output


def _filled_smoothed(values: list[float | None], max_gap: int = 3) -> list[float | None]:
    """Заполняет короткие пропуски линейно и подавляет дрожание landmarks."""
    result = values[:]
    points = [i for i, value in enumerate(result) if value is not None]
    for a, b in zip(points, points[1:]):
        if 1 < b - a <= max_gap + 1:
            va, vb = result[a], result[b]
            for i in range(a + 1, b):
                result[i] = va + (vb - va) * (i - a) / (b - a)
    # Медиана из трёх кадров; длинные пропуски остаются разрывами.
    median_filtered = []
    for i, value in enumerate(result):
        if value is None:
            median_filtered.append(None)
            continue
        neighbors = [result[j] for j in range(max(0, i - 1), min(len(result), i + 2)) if result[j] is not None]
        median_filtered.append(median(neighbors))
    return median_filtered


def _percentile(values: list[float], fraction: float) -> float:
    sorted_values = sorted(values)
    position = (len(sorted_values) - 1) * fraction
    lo = int(position)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] * (hi - position) + sorted_values[hi] * (position - lo) if hi != lo else sorted_values[lo]


def _count_cycles(values: list[float | None], fps: float, kind: str) -> tuple[int, float]:
    """Считает устойчивые циклы верх–низ–верх с гистерезисом.

    Возвращает (количество, качество сигнала). Качество не является
    вероятностью и не должно показываться как процент точности.
    """
    actual = [v for v in values if v is not None and isfinite(v)]
    if len(actual) < max(10, int(fps * 1.5)):
        return 0, 0.0

    # Адаптируемся к масштабу движения, но не принимаем мелкий шум за повтор.
    span = _percentile(actual, 0.90) - _percentile(actual, 0.10)
    minimum_span = 23.0 if kind.startswith("elbow") else 0.30
    if span < minimum_span:
        return 0, 0.0

    low = _percentile(actual, 0.20)
    high = _percentile(actual, 0.80)
    delta = high - low
    if delta < minimum_span * 0.60:
        return 0, 0.0
    up_threshold = low + delta * 0.32
    down_threshold = low + delta * 0.68

    state = "unknown"
    up_streak = down_streak = 0
    last_up_frame = None
    bottom_frame = None
    reps = 0
    gap = 0
    min_half = max(2, round(fps * 0.18))
    min_cycle = max(4, round(fps * 0.50))
    max_cycle = round(fps * 12)

    for i, value in enumerate(values):
        if value is None:
            gap += 1
            up_streak = down_streak = 0
            if gap >= max(5, int(fps * 0.8)):
                state, last_up_frame, bottom_frame = "unknown", None, None
            continue
        gap = 0
        up_streak = up_streak + 1 if value <= up_threshold else 0
        down_streak = down_streak + 1 if value >= down_threshold else 0

        if state == "unknown":
            if up_streak >= min_half:
                state = "up"
                last_up_frame = i
        elif state == "up" and down_streak >= min_half:
            state = "down"
            bottom_frame = i
        elif state == "down" and up_streak >= min_half:
            if last_up_frame is not None and bottom_frame is not None:
                duration = i - last_up_frame
                if min_cycle <= duration <= max_cycle:
                    reps += 1
            state = "up"
            last_up_frame = i
            bottom_frame = None

    coverage = len(actual) / len(values) if values else 0.0
    strength = min(span / (minimum_span * 2.0), 1.0)
    return reps, coverage * strength


def _pick_count(signals: dict[str, list[float | None]], sample_fps: float) -> tuple[int, float]:
    """Выбирает наиболее надёжный признак; не суммирует повторы разных каналов."""
    choices = []
    for kind, readings in signals.items():
        smoothed = _filled_smoothed(readings)
        count, quality = _count_cycles(smoothed, sample_fps, kind)
        # Слегка предпочитаем локтевые углы движениям корпуса.
        priority = 1.10 if kind == "elbow_3d" else 1.05 if kind == "elbow_2d" else 1.0
        choices.append((quality * priority, count, kind))
    best = max(choices, default=(0.0, 0, ""))
    return best[1], best[0]


def analyze_video(path: str | Path, target_fps: int = 10) -> Analysis | None:
    """Анализирует видео. None — недостаточно данных для автоматического подсчёта.

    FPS ограничен для VPS с 2 vCPU; ноги не требуются. Анализ выполняется
    синхронно, вызывающий код может запускать его через asyncio.to_thread().
    """
    import cv2
    import mediapipe as mp

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None

    raw_fps = cap.get(cv2.CAP_PROP_FPS)
    raw_fps = raw_fps if 1 <= raw_fps <= 120 else 25.0
    frame_step = max(1, round(raw_fps / max(1, target_fps)))
    sample_fps = raw_fps / frame_step
    sampled = valid = index = 0
    signals: dict[str, list[float | None]] = {"elbow_2d": [], "elbow_3d": [], "body": []}
    pose_api = mp.solutions.pose

    try:
        with pose_api.Pose(
            static_image_mode=False,
            model_complexity=1,
            smooth_landmarks=True,
            min_detection_confidence=0.50,
            min_tracking_confidence=0.60,
        ) as pose:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if index % frame_step != 0:
                    index += 1
                    continue
                index += 1
                sampled += 1
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                rgb.flags.writeable = False
                result = pose.process(rgb)
                features = (
                    _extract_signals(result.pose_landmarks, result.pose_world_landmarks, pose_api.PoseLandmark)
                    if result.pose_landmarks is not None else {}
                )
                if features:
                    valid += 1
                for key in signals:
                    signals[key].append(features.get(key))
    finally:
        cap.release()

    if sampled < 10 or valid < 8 or valid / sampled < 0.35:
        return None

    count, quality = _pick_count(signals, sample_fps)
    # Без устойчивого движения лучше запросить ручной ввод, чем сохранить 0.
    if quality < 0.22:
        return None
    return Analysis(repetitions=count, valid_frames=valid, sampled_frames=sampled)
