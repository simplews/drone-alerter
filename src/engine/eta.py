"""Hybrid ETA calculation and danger level selection."""
from __future__ import annotations

from datetime import datetime, timedelta

from src.engine.models import AlertLevel, DroneType
from src.learning.calibration import physics_interval
from src.learning.reference import PointRef, ReferenceData
from src.utils.time import format_kyiv_time


def calculate_eta(
    point: PointRef,
    ref: ReferenceData,
    post_time_utc: datetime,
    current_time_utc: datetime,
    drone_type: DroneType = DroneType.JET,
) -> tuple[float, float, datetime, datetime, str]:
    """Calculate the minimum and maximum approach time (in minutes) and the time window.

    Returns:
        (rem_min_minutes, rem_max_minutes, window_start_utc, window_end_utc, window_str)
    """
    elapsed_minutes = max(0.0, (current_time_utc - post_time_utc).total_seconds() / 60.0)

    # 1. Find the base approach range from the point
    if drone_type == DroneType.MISSILE:
        if point.distance_km:
            missile_speed = getattr(ref, "missile_speed_kmh", [750.0, 880.0])
            base_min, base_max = physics_interval(point.distance_km, missile_speed, ref.post_lag_min)
            base_min = round(base_min)
            base_max = round(base_max)
        else:
            base_min, base_max = 3.0, 10.0
    elif drone_type == DroneType.CONVENTIONAL:
        if point.distance_km:
            conv_speed = getattr(ref, "conventional_speed_kmh", [150.0, 185.0])
            base_min, base_max = physics_interval(point.distance_km, conv_speed, ref.post_lag_min)
            base_min = round(base_min)
            base_max = round(base_max)
        else:
            base_min, base_max = 15.0, 45.0
    elif point.eta_min is not None and point.eta_max is not None:
        base_min = point.eta_min
        base_max = point.eta_max
    elif point.distance_km:
        base_min, base_max = physics_interval(point.distance_km, ref.speed_kmh, ref.post_lag_min)
    else:
        # Safe default
        base_min, base_max = 5.0, 15.0

    # 2. Calculate the absolute time window near the city
    window_start = post_time_utc + timedelta(minutes=base_min)
    window_end = post_time_utc + timedelta(minutes=base_max)

    # 3. Calculate the time that remains from now
    rem_min = max(0.0, base_min - elapsed_minutes)
    rem_max = max(0.0, base_max - elapsed_minutes)

    # 4. Format for the user in Kyiv time (Europe/Kyiv)
    # Format: "22:50–22:58 (за 25–33 хв)"
    start_str = format_kyiv_time(window_start)
    end_str = format_kyiv_time(window_end)
    min_int = round(rem_min)
    max_int = round(rem_max)

    if getattr(point, "is_regional", False):
        if min_int <= 0 and max_int <= 0:
            window_str = "у разі повороту на місто: менше 2 хв"
        else:
            window_str = f"у разі повороту на місто: ~{min_int}–{max_int} хв ({start_str}–{end_str})"
    elif max_int <= 2:
        window_str = f"{start_str}–{end_str} (менше 2 хв / над містом зараз)"
    else:
        window_str = f"{start_str}–{end_str} (за {min_int}–{max_int} хв)"

    return rem_min, rem_max, window_start, window_end, window_str


LEVEL_3B_POINT_IDS = frozenset({
    "tysmenytsia",
    "pidpechery",
    "yamnytsia",
    "cherniiv",
    "drahomyrchany",
    "khryplyn",
    "kluziv",
    "vovchyntsi",
    "uhryniv",
    "mykytyntsi",
    "krykhivtsi",
    "ivano_frankivsk",
})


def determine_level(point_id: str, point_level: int, is_regional: bool = False) -> AlertLevel:
    """Select the alert level from the point and its level in the reference."""
    if is_regional:
        return AlertLevel.REGIONAL
    if point_id in LEVEL_3B_POINT_IDS:
        return AlertLevel.LEVEL_3B
    if point_id in ("chernelytsia", "tlumach") or point_level == 3:
        return AlertLevel.LEVEL_3A
    if point_level == 2:
        return AlertLevel.LEVEL_2
    return AlertLevel.LEVEL_1
