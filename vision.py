"""Оценка повторяющихся движений в видеокружочке без MediaPipe.

OpenCV отслеживает контрастные точки (оптический поток Lucas–Kanade),
ищет согласованные циклы их перемещения по X, Y или изменения расстояния
до центра кадра. Подходит для экспериментов с фронтальной/боковой съёмкой,
но НЕ может доказать, что обнаруженное движение — именно отжимание. Скорее как счетчик сделан.

Если надёжного согласия точек нет, возвращает None: бот запросит ручной ввод.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Analysis:
    repetitions: int
    valid_frames: int
    sampled_frames: int


@dataclass
class _Track:
    """История одной особенности изображения (в координатах кадра)."""

    observations: list[tuple[int, float, float]] = field(default_factory=list)


def _smooth(values):
    """Сглаживание, не искажающее края короткой последовательности."""
    import numpy as np

    if len(values) < 5:
        return values
    padded = np.pad(values, (2, 2), mode="edge")
    return np.convolve(padded, np.array([1, 2, 3, 2, 1]) / 9, mode="valid")


def _count_cycles(values, fps: float, min_amplitude: float = 2.5) -> tuple[int, float]:
    """Количество полных циклов «край A → край B → край A».

    Порог определяется амплитудой движения конкретной точки, а не
    абсолютной величиной её координаты. Считаем в обоих направлениях.
    """
    import numpy as np

    if len(values) < max(12, round(fps * 1.8)):
        return 0, 0.0
    smoothed = _smooth(np.asarray(values, dtype=np.float64))
    lo, hi = np.percentile(smoothed, [20, 80])
    span = float(hi - lo)
    if span < min_amplitude:
        return 0, 0.0

    low = lo + 0.20 * span
    high = hi - 0.20 * span
    # Два наблюдения подряд: защита от кратковременных скачков трекера.
    dwell = max(2, round(fps * 0.13))
    min_cycle = round(fps * 0.55)
    max_cycle = round(fps * 10)

    state = None
    last_endpoint_index = None
    endpoints: list[int] = []
    streak_low = streak_high = 0
    min_half = max(2, round(fps * 0.17))

    for index, value in enumerate(smoothed):
        streak_low = streak_low + 1 if value <= low else 0
        streak_high = streak_high + 1 if value >= high else 0
        endpoint = "low" if streak_low >= dwell else "high" if streak_high >= dwell else None
        if endpoint is None or endpoint == state:
            continue
        if last_endpoint_index is not None and index - last_endpoint_index < min_half:
            continue
        state = endpoint
        last_endpoint_index = index
        endpoints.append(index)

    # Считаем по паре противоположных крайних положений на цикл.
    # Это устойчивее к записи, начавшейся посередине первого повтора.
    if len(endpoints) < 3:
        return 0, 0.0
    full_cycle_durations = [endpoints[i + 2] - endpoints[i] for i in range(len(endpoints) - 2)]
    correct = [min_cycle <= duration <= max_cycle for duration in full_cycle_durations]
    if sum(correct) < max(1, int(0.70 * len(correct))):
        return 0, 0.0
    count = len(endpoints) // 2

    return count, min(span / (min_amplitude * 3.0), 1.0)


def _choose_count(tracks: dict[int, _Track], frames: int, fps: float, size: tuple[int, int]) -> int | None:
    """Голосование движущихся точек. Неподвижный фон не участвует."""
    import numpy as np

    w, h = size
    votes: list[tuple[int, float]] = []
    for track in tracks.values():
        history = track.observations
        if len(history) < max(round(2.5 * fps), round(frames * 0.60)):
            continue
        # Не экстраполируем по короткому фрагменту на всё видео.
        if history[0][0] > frames * 0.12 or history[-1][0] < frames * 0.88:
            continue

        _, x, y = np.array(history, dtype=float).T
        radius = np.hypot(x - w / 2.0, y - h / 2.0)
        choices = [_count_cycles(series, fps) for series in (x, y, radius)]
        # Предпочитаем наиболее выраженный периодический сигнал.
        best_count, quality = max(choices, key=lambda result: (result[0] > 0, result[1], result[0]))
        if best_count > 0 and quality >= 0.35:
            votes.append((best_count, quality))

    if len(votes) < 6:
        return None

    counts = np.asarray([count for count, _ in votes], dtype=int)
    # В оптическом потоке соседние точки иногда пропускают один цикл.
    # Не считаем «лидера» — берём медиану независимых наблюдений.
    estimate = int(round(float(np.median(counts))))
    tolerance = max(1, round(estimate * 0.18))
    supporters = sum(abs(count - estimate) <= tolerance for count in counts)
    if estimate < 1 or supporters < max(6, round(len(votes) * 0.60)):
        return None
    return estimate


def analyze_video(path: str | Path, target_fps: int = 10) -> Analysis | None:
    """Оценивает циклы движения, возвращает None при неоднозначном видео.

    Сигнатура и структура Analysis совместимы с существующим bot.py.
    Видеофайл не загружается в сторонние сервисы.
    """
    import cv2
    import numpy as np

    video = cv2.VideoCapture(str(path))
    if not video.isOpened():
        return None

    source_fps = video.get(cv2.CAP_PROP_FPS)
    source_fps = source_fps if 1 <= source_fps <= 120 else 25.0
    step = max(1, round(source_fps / max(1, target_fps)))
    effective_fps = source_fps / step
    index = 0
    sampled = 0
    valid = 0
    previous = None
    prev_points = None
    point_ids: list[int] = []
    tracks: dict[int, _Track] = {}
    next_id = 0
    image_size = None
    mask = None

    def add_points(gray, active):
        nonlocal next_id
        # Ищем новые особенности на теле, если ранее отслеживаемые исчезли.
        current_mask = mask.copy()
        if len(active):
            for x, y in active.reshape(-1, 2):
                cv2.circle(current_mask, (int(x), int(y)), 7, 0, thickness=-1)
        candidates = cv2.goodFeaturesToTrack(
            gray, maxCorners=min(200 - len(active), 120),
            qualityLevel=0.012, minDistance=6, mask=current_mask, blockSize=5,
        )
        if candidates is None:
            return active, []
        new_ids = []
        for candidate in candidates.reshape(-1, 2):
            tracks[next_id] = _Track([(sampled - 1, float(candidate[0]), float(candidate[1]))])
            new_ids.append(next_id)
            next_id += 1
        appended = candidates.astype(np.float32).reshape(-1, 1, 2)
        return (np.concatenate([active, appended], axis=0) if len(active) else appended), new_ids

    try:
        while True:
            ok, frame = video.read()
            if not ok:
                break
            index += 1
            if (index - 1) % step:
                continue
            sampled += 1
            height, width = frame.shape[:2]
            if min(width, height) <= 0:
                continue
            scale = 300 / max(width, height)
            if scale < 1:
                frame = cv2.resize(frame, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (3, 3), 0)

            if previous is None:
                h, w = gray.shape
                image_size = (w, h)
                mask = np.zeros((h, w), dtype=np.uint8)
                # У видеокружочков края обычно чёрные; работаем в центре круга.
                cv2.ellipse(mask, (w // 2, h // 2), (int(w * 0.42), int(h * 0.42)), 0, 0, 360, 255, -1)
                prev_points = np.empty((0, 1, 2), dtype=np.float32)
                prev_points, point_ids = add_points(gray, prev_points)
                previous = gray
                if len(point_ids) >= 10:
                    valid += 1
                continue

            if gray.shape != previous.shape:
                break

            surviving = np.empty((0, 1, 2), dtype=np.float32)
            surviving_ids: list[int] = []
            if len(prev_points):
                moved, status, _ = cv2.calcOpticalFlowPyrLK(
                    previous, gray, prev_points, None,
                    winSize=(21, 21), maxLevel=2,
                    criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 22, 0.02),
                )
                if moved is not None and status is not None:
                    backward, back_status, _ = cv2.calcOpticalFlowPyrLK(
                        gray, previous, moved, None,
                        winSize=(21, 21), maxLevel=2,
                        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 22, 0.02),
                    )
                    if backward is not None and back_status is not None:
                        fb_error = np.linalg.norm(prev_points.reshape(-1, 2) - backward.reshape(-1, 2), axis=1)
                        okay = (
                            (status.reshape(-1) == 1) &
                            (back_status.reshape(-1) == 1) &
                            (fb_error < 2.0) &
                            np.isfinite(moved.reshape(-1, 2)).all(axis=1)
                        )
                        candidates = moved.reshape(-1, 2)
                        h, w = gray.shape
                        okay &= (candidates[:, 0] >= 4) & (candidates[:, 0] < w - 4)
                        okay &= (candidates[:, 1] >= 4) & (candidates[:, 1] < h - 4)
                        surviving = moved[okay].reshape(-1, 1, 2)
                        surviving_ids = [point_id for point_id, good in zip(point_ids, okay) if good]
                        for point_id, (x, y) in zip(surviving_ids, surviving.reshape(-1, 2)):
                            tracks[point_id].observations.append((sampled - 1, float(x), float(y)))

            if len(surviving_ids) >= 10:
                valid += 1
            if len(surviving_ids) < 85 and sampled % max(4, round(effective_fps)) == 0:
                surviving, added_ids = add_points(gray, surviving)
                surviving_ids.extend(added_ids)
            prev_points = surviving
            point_ids = surviving_ids
            previous = gray
    finally:
        video.release()

    if sampled < max(12, round(effective_fps * 2.5)) or valid / max(1, sampled) < 0.65:
        return None
    if image_size is None:
        return None
    count = _choose_count(tracks, sampled, effective_fps, image_size)
    if count is None:
        return None
    return Analysis(repetitions=count, valid_frames=valid, sampled_frames=sampled)
