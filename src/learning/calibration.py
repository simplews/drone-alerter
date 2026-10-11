"""Калібрування таблиці ETA і базових частот за розміченими даними.

Принципи (див. docs/LEARNING.md):
1. Поки даних мало (n < min_obs_empirical), нові спостереження можуть
   лише РОЗШИРИТИ діапазон: об'єднуємо їх із попереднім діапазоном і
   фізичною оцінкою (відстань / швидкість).
2. Коли даних достатньо, переходимо на емпіричні квантилі, але звужувати
   діапазон дозволено не більше ніж на N хв за одне оновлення.
   Розширювати — без обмежень (недооцінка часу безпечніша за переоцінку).
3. Межі округлюються назовні: нижня вниз, верхня вгору.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from .reference import PointRef
from .store import (
    OUTCOME_DIVERTED,
    OUTCOME_DOWNED,
    OUTCOME_REACHED,
    ArrivalObservation,
    OutcomeRow,
)


@dataclass
class CalibrationConfig:
    min_obs_empirical: int = 6
    q_lo: float = 0.15
    q_hi: float = 0.85
    margin_min: float = 1.0
    max_narrowing_per_update_min: float = 6.0
    min_speed_obs: int = 4
    # Beta(a, b) апріорі для P(ІФ | пункт). (1, 1) — рівномірний.
    base_rate_prior: tuple[float, float] = (1.0, 1.0)


def quantile(values: list[float], q: float) -> float:
    data = sorted(values)
    if not data:
        raise ValueError("порожній список")
    pos = (len(data) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def physics_interval(distance_km: float, speed_kmh: list[float], lag_min: list[float]) -> tuple[float, float]:
    v_min, v_max = speed_kmh
    lag_max = lag_min[1]
    lo = distance_km / v_max * 60 - lag_max
    hi = distance_km / v_min * 60 + lag_max
    return max(0.0, lo), hi


def fit_speed(observations: list[ArrivalObservation], points: dict[str, PointRef],
              cfg: CalibrationConfig) -> list[float] | None:
    """Діапазон швидкості з усіх спостережень, де відома відстань пункту."""
    speeds = []
    for obs in observations:
        point = points.get(obs.point_id)
        if point and point.distance_km and obs.minutes_to_arrival > 0:
            speeds.append(point.distance_km / (obs.minutes_to_arrival / 60))
    if len(speeds) < cfg.min_speed_obs:
        return None
    return [round(quantile(speeds, cfg.q_lo)), round(quantile(speeds, cfg.q_hi))]


def _union(a: tuple[float, float] | None, b: tuple[float, float] | None) -> tuple[float, float] | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a[0], b[0]), max(a[1], b[1])


def calibrate_point(point: PointRef, durations: list[float], speed_kmh: list[float],
                    lag_min: list[float], cfg: CalibrationConfig) -> tuple[float, float, str] | None:
    """Новий діапазон ETA для пункту або None, якщо оцінити нема з чого."""
    prev = (point.eta_min, point.eta_max) if point.eta_min is not None and point.eta_max is not None else None
    physics = physics_interval(point.distance_km, speed_kmh, lag_min) if point.distance_km else None
    n = len(durations)

    if n == 0:
        if prev is not None:
            return None  # нових даних немає — нічого не змінюємо
        if physics is None:
            return None
        lo, hi, method = physics[0], physics[1], "physics"
    elif n < cfg.min_obs_empirical:
        lo = min(durations) - cfg.margin_min
        hi = max(durations) + cfg.margin_min
        merged = _union((lo, hi), physics) if physics else _union((lo, hi), prev)
        lo, hi = merged
        if physics:
            hi = min(hi, physics[1] + 1.5)
            lo = max(lo, max(0.0, physics[0] - 1.0))
        method = "minmax+margin+prior" if (prev or physics) else "minmax+margin"
    else:
        lo = quantile(durations, cfg.q_lo) - cfg.margin_min
        hi = quantile(durations, cfg.q_hi) + cfg.margin_min
        method = "empirical_q"
        if physics:
            hi = min(hi, physics[1] + 1.5)
            lo = max(lo, max(0.0, physics[0] - 1.0))
        if prev is not None:
            step = cfg.max_narrowing_per_update_min
            if lo > prev[0]:
                lo = min(lo, prev[0] + step)
            if hi < prev[1]:
                hi = max(hi, prev[1] - step)

    lo = max(0.0, float(math.floor(lo)))
    hi = float(math.ceil(hi))
    return lo, hi, method



def base_rates(rows: list[OutcomeRow], cfg: CalibrationConfig) -> dict[str, tuple[float, int]]:
    """P(ціль іде на ІФ | згадка пункту). Позитив — долетіла або збита на шляху до ІФ,
    негатив — пішла в іншу область. unknown не враховується."""
    pos: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    for row in rows:
        if row.outcome in (OUTCOME_REACHED, OUTCOME_DOWNED):
            pos[row.point_id] += 1
            total[row.point_id] += 1
        elif row.outcome == OUTCOME_DIVERTED:
            total[row.point_id] += 1
    a, b = cfg.base_rate_prior
    return {pid: (round((pos[pid] + a) / (n + a + b), 3), n) for pid, n in total.items()}


def evaluate(points: dict[str, PointRef], observations: list[ArrivalObservation]) -> dict:
    """Покриття: частка спостережень, у яких фактичний час потрапив у діапазон ETA."""
    hits = 0
    widths: list[float] = []
    per_point: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for obs in observations:
        p = points.get(obs.point_id)
        if p is None or p.eta_min is None or p.eta_max is None:
            continue
        hit = p.eta_min <= obs.minutes_to_arrival <= p.eta_max
        hits += hit
        widths.append(p.eta_max - p.eta_min)
        per_point[obs.point_id][0] += hit
        per_point[obs.point_id][1] += 1
    n = len(widths)
    return {
        "n": n,
        "coverage": round(hits / n, 3) if n else None,
        "mean_width_min": round(sum(widths) / n, 1) if n else None,
        "per_point": {pid: {"hits": h, "n": t} for pid, (h, t) in per_point.items()},
    }
