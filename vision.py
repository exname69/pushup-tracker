"""Подсчёт полных отжиманий из локального видео.

Эвристика рассчитана на запись сбоку: плечо, локоть, запястье,
таз и колено должны находиться в кадре. Не является арбитром техники.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import acos, degrees, hypot
from pathlib import Path


@dataclass(frozen=True)
class Analysis:
    repetitions: int
    valid_frames: int
    sampled_frames: int


class PushupCounter:
    """Автомат состояний: вверх -> вниз -> вверх = 1 повтор.

    Два последовательных наблюдения на каждое переключение отсеивают
    часть флуктуаций детектора; большие разрывы сбрасывают цикл.
    """

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
            if self._missing >= 8:  # ~1 с без уверенных точек тела
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
    """Угол ABC в градусах; b — вершина."""
    ba = (a[0] - b[0], a[1] - b[1])
    bc = (c[0] - b[0], c[1] - b[1])
    norm = hypot(*ba) * hypot(*bc)
    if norm < 1e-8:
        return 0.0
    cos_value = (ba[0] * bc[0] + ba[1] * bc[1]) / norm
    return degrees(acos(max(-1.0, min(1.0, cos_value))))


def _frame_elbow_angle(landmarks, indices) -> float | None:
    """Выбираем сторону тела с наиболее уверенными landmarks."""
    best: tuple[float, float] | None = None
    for side in ("LEFT", "RIGHT"):
        names = [f"{side}_{part}" for part in ("SHOULDER", "ELBOW", "WRIST", "HIP", "KNEE")]
        points = [landmarks.landmark[indices[name].value] for name in names]
        confidence = min(p.visibility for p in points)
        if confidence < 0.50:
            continue
        shoulder, elbow, wrist, hip, knee = [(p.x, p.y) for p in points]
        torso_angle = joint_angle(shoulder, hip, knee)
        if torso_angle < 140:  # отсекаем приседания, сильный прогиб и посторонние движения
            continue
        arm_angle = joint_angle(shoulder, elbow, wrist)
        if best is None or confidence > best[0]:
            best = (confidence, arm_angle)
    return None if best is None else best[1]


def analyze_video(path: str | Path, target_fps: int = 8) -> Analysis | None:
    """Синхронная CPU-задача. None, если поза не определяется надёжно."""
    # Импорт ленивый: unit-тесты счётчика не требуют ML-библиотек.
    import cv2
    import mediapipe as mp

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None

    fps = cap.get(cv2.CAP_PROP_FPS)
    fps = fps if 1 <= fps <= 120 else 25.0
    frame_step = max(1, round(fps / target_fps))
    counter = PushupCounter()
    sampled = valid = index = 0
    pose_api = mp.solutions.pose  # Совместимо с закреплённым mediapipe==0.10.21

    try:
        with pose_api.Pose(
            static_image_mode=False,
            model_complexity=1,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
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
                angle = None
                if result.pose_landmarks is not None:
                    angle = _frame_elbow_angle(result.pose_landmarks, pose_api.PoseLandmark)
                if angle is not None:
                    valid += 1
                counter.update(angle)
    finally:
        cap.release()

    # 0 — допустимый результат только при достаточно уверенном распознавании.
    if sampled < 6 or valid < 6 or valid / sampled < 0.30:
        return None
    return Analysis(repetitions=counter.repetitions, valid_frames=valid, sampled_frames=sampled)
