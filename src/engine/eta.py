"""Розрахунок гібридного ETA та визначення рівнів небезпеки."""
from __future__ import annotations

from datetime import datetime, timedelta

from src.engine.models import AlertLevel
from src.learning.calibration import physics_interval
from src.learning.reference import PointRef, ReferenceData
from src.utils.time import format_kyiv_time


def calculate_eta(
    point: PointRef,
    ref: ReferenceData,
    post_time_utc: datetime,
    current_time_utc: datetime,
) -> tuple[float, float, datetime, datetime, str]:
    """Обчислює мінімальний і максимальний час підльоту (у хвилинах) та часове вікно.

    Повертає:
        (rem_min_minutes, rem_max_minutes, window_start_utc, window_end_utc, window_str)
    """
    elapsed_minutes = max(0.0, (current_time_utc - post_time_utc).total_seconds() / 60.0)

    # 1. Визначаємо базовий діапазон підльоту від пункту
    if point.eta_min is not None and point.eta_max is not None:
        base_min = point.eta_min
        base_max = point.eta_max
    elif point.distance_km:
        base_min, base_max = physics_interval(point.distance_km, ref.speed_kmh, ref.post_lag_min)
    else:
        # Безпечний дефолт
        base_min, base_max = 5.0, 15.0

    # 2. Розрахунок абсолютного часового вікна біля міста
    window_start = post_time_utc + timedelta(minutes=base_min)
    window_end = post_time_utc + timedelta(minutes=base_max)

    # 3. Залишковий час від поточного моменту
    rem_min = max(0.0, base_min - elapsed_minutes)
    rem_max = max(0.0, base_max - elapsed_minutes)

    # 4. Форматування для користувача за київським часом (Europe/Kyiv)
    # Формат: "22:50–22:58 (за 25–33 хв)"
    start_str = format_kyiv_time(window_start)
    end_str = format_kyiv_time(window_end)
    min_int = round(rem_min)
    max_int = round(rem_max)

    if max_int <= 2:
        window_str = f"{start_str}–{end_str} (менше 2 хв / над містом зараз)"
    else:
        window_str = f"{start_str}–{end_str} (за {min_int}–{max_int} хв)"

    return rem_min, rem_max, window_start, window_end, window_str


def determine_level(point_id: str, point_level: int) -> AlertLevel:
    """Визначає рівень сповіщення на основі пункту та його рівня в довіднику."""
    if point_id in ("tysmenytsia", "khryplyn"):
        return AlertLevel.LEVEL_3B
    if point_id in ("chernelytsia", "tlumach") or point_level == 3:
        return AlertLevel.LEVEL_3A
    if point_level == 2:
        return AlertLevel.LEVEL_2
    return AlertLevel.LEVEL_1
