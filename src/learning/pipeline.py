"""Цикл перенавчання: дані -> кандидат-версія -> перевірка -> (схвалення) -> активна.

Запускається пакетно (раз на добу або вручну), а не онлайн після кожного
поста: ETA не повинен змінюватися посеред атаки, поки сповіщення вже йдуть.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .calibration import (
    CalibrationConfig,
    base_rates,
    calibrate_point,
    evaluate,
    fit_speed,
)
from .reference import PointRef, ReferenceData, ReferenceRepository
from .store import AliasCandidate, ArrivalObservation, LearningStore, OutcomeRow
from .text import normalize


@dataclass
class LearningConfig:
    # Не перенавчати, якщо є нерозмічені цілі, що з'явилися за останні N год.
    freeze_window_hours: float = 3.0
    # Частка найсвіжіших цілей, на яких перевіряється нова версія.
    holdout_fraction: float = 0.3
    # Менше цілей у перевірці — автоматичне просування заборонене.
    min_holdout_targets: int = 3
    # Нова версія не може розширити середній діапазон більше ніж у стільки разів,
    # якщо при цьому не покращує покриття.
    max_width_growth: float = 1.5
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)


@dataclass
class RetrainReport:
    status: str  # skipped | pending | promoted
    reason: str
    version: int | None = None
    changelog: list[str] = field(default_factory=list)
    gates: dict = field(default_factory=dict)
    holdout: dict = field(default_factory=dict)


def build_candidate(active: ReferenceData, observations: list[ArrivalObservation],
                    outcomes: list[OutcomeRow], aliases: list[AliasCandidate],
                    cfg: CalibrationConfig) -> ReferenceData:
    cand = active.clone_as_candidate()
    log = cand.changelog

    # 1. Схвалені людиною назви.
    index = cand.alias_index()
    for alias in aliases:
        pid = alias.decided_point_id
        if not pid:
            continue
        if pid not in cand.points:
            cand.points[pid] = PointRef(id=pid, names=[], level=alias.new_point_level or 1)
            log.append(f"Новий пункт {pid} (рівень {cand.points[pid].level})")
        for form in [alias.phrase_norm, *alias.surface_forms]:
            norm = normalize(form)
            if norm not in index:
                cand.points[pid].names.append(norm)
                index[norm] = pid
                log.append(f"Назва «{norm}» -> {pid}")

    # 2. Швидкість.
    speed = fit_speed(observations, cand.points, cfg)
    if speed and speed != cand.speed_kmh:
        log.append(f"Швидкість {cand.speed_kmh} -> {speed} км/год")
        cand.speed_kmh = speed

    # 3. ETA по пунктах.
    durations: dict[str, list[float]] = defaultdict(list)
    for obs in observations:
        durations[obs.point_id].append(obs.minutes_to_arrival)
    for pid, point in cand.points.items():
        result = calibrate_point(point, durations.get(pid, []), cand.speed_kmh, cand.post_lag_min, cfg)
        if result is None:
            continue
        lo, hi, method = result
        if (lo, hi) != (point.eta_min, point.eta_max):
            log.append(f"ETA {pid}: {point.eta_min}–{point.eta_max} -> {lo:g}–{hi:g} хв ({method})")
        point.eta_min, point.eta_max, point.eta_method = lo, hi, method
        if durations.get(pid):
            point.n_obs = len(durations[pid])

    # 4. Базові частоти P(ІФ | пункт).
    for pid, (rate, n) in base_rates(outcomes, cfg).items():
        if pid not in cand.points:
            continue
        point = cand.points[pid]
        if point.base_rate != rate:
            log.append(f"P(ІФ | {pid}): {point.base_rate} -> {rate} (n={n})")
        point.base_rate, point.n_outcomes = rate, n

    return cand


def _split_by_time(observations: list[ArrivalObservation], first_seen: dict[str, datetime],
                   holdout_fraction: float) -> tuple[list[ArrivalObservation], list[ArrivalObservation], int]:
    target_ids = sorted({o.target_id for o in observations}, key=lambda t: first_seen[t])
    n_test = round(len(target_ids) * holdout_fraction)
    test_ids = set(target_ids[len(target_ids) - n_test:]) if n_test else set()
    train = [o for o in observations if o.target_id not in test_ids]
    test = [o for o in observations if o.target_id in test_ids]
    return train, test, len(test_ids)


def retrain(store: LearningStore, repo: ReferenceRepository, cfg: LearningConfig | None = None,
            auto_promote: bool = False, now: datetime | None = None) -> RetrainReport:
    cfg = cfg or LearningConfig()
    now = now or datetime.now(UTC)

    if store.has_open_targets(now, cfg.freeze_window_hours):
        return RetrainReport("skipped", "Є нерозмічені цілі за останні "
                             f"{cfg.freeze_window_hours:g} год — оновлення відкладено")

    active = repo.active()
    observations = store.arrival_observations()
    outcomes = store.outcome_rows()
    aliases = store.alias_candidates(status="approved")

    known = active.alias_index()
    new_aliases = any(
        normalize(form) not in known
        for a in aliases for form in [a.phrase_norm, *a.surface_forms]
    )
    new_labels = store.count_labeled() != active.metrics.get("n_labeled_targets", -1)
    if not new_labels and not new_aliases:
        return RetrainReport("skipped", "Нових розмічених цілей і схвалених назв немає")

    candidate = build_candidate(active, observations, outcomes, aliases, cfg.calibration)
    if not candidate.changelog:
        return RetrainReport("skipped", "Нових змін немає")

    # Перевірка процедури на найсвіжіших цілях: вчимося на старших, міряємо на новіших.
    train, test, n_test_targets = _split_by_time(observations, store.target_first_seen(), cfg.holdout_fraction)
    trial = build_candidate(active, train, outcomes, aliases, cfg.calibration)
    m_active = evaluate(active.points, test)
    m_trial = evaluate(trial.points, test)

    cov_a, cov_t = m_active["coverage"], m_trial["coverage"]
    width_a, width_t = m_active["mean_width_min"], m_trial["mean_width_min"]
    gates = {
        "enough_holdout": n_test_targets >= cfg.min_holdout_targets,
        "coverage_not_worse": cov_t is not None and (cov_a is None or cov_t >= cov_a),
        "width_ok": (
            width_t is None or not width_a
            or width_t <= width_a * cfg.max_width_growth
            or (cov_a is not None and cov_t is not None and cov_t > cov_a)
        ),
    }
    holdout = {"targets": n_test_targets, "active": m_active, "candidate": m_trial}

    candidate.metrics = {
        "n_labeled_targets": store.count_labeled(),
        "n_arrival_obs": len(observations),
        "fit_on_all": evaluate(candidate.points, observations),
        "holdout": holdout,
        "gates": gates,
    }
    candidate = repo.save_new(candidate)

    if auto_promote and all(gates.values()):
        repo.promote(candidate.version)
        return RetrainReport("promoted", "Усі перевірки пройдено", candidate.version,
                             candidate.changelog, gates, holdout)
    failed = [k for k, ok in gates.items() if not ok]
    reason = ("Очікує схвалення" if not failed
              else "Очікує схвалення; не пройдено: " + ", ".join(failed))
    return RetrainReport("pending", reason, candidate.version, candidate.changelog, gates, holdout)


def diff(old: ReferenceData, new: ReferenceData) -> list[str]:
    """Людиночитна різниця між двома версіями (для перегляду перед схваленням)."""
    lines: list[str] = []
    for pid in sorted(set(old.points) | set(new.points)):
        a, b = old.points.get(pid), new.points.get(pid)
        if a is None:
            lines.append(f"+ пункт {pid}: {b.names}, рівень {b.level}")
            continue
        if b is None:
            lines.append(f"- пункт {pid}")
            continue
        added = sorted(set(b.names) - set(a.names))
        removed = sorted(set(a.names) - set(b.names))
        if added:
            lines.append(f"  {pid}: + назви {added}")
        if removed:
            lines.append(f"  {pid}: - назви {removed}")
        if (a.eta_min, a.eta_max) != (b.eta_min, b.eta_max):
            lines.append(f"  {pid}: ETA {a.eta_min}–{a.eta_max} -> {b.eta_min}–{b.eta_max} ({b.eta_method}, n={b.n_obs})")
        if a.level != b.level:
            lines.append(f"  {pid}: рівень {a.level} -> {b.level}")
        if a.distance_km != b.distance_km:
            lines.append(f"  {pid}: відстань {a.distance_km} -> {b.distance_km} км")
        if a.base_rate != b.base_rate:
            lines.append(f"  {pid}: P(ІФ) {a.base_rate} -> {b.base_rate} (n={b.n_outcomes})")
    if old.speed_kmh != new.speed_kmh:
        lines.append(f"  швидкість {old.speed_kmh} -> {new.speed_kmh} км/год")
    for attr, label in (("target_keywords", "ключові слова цілі"), ("cancel_keywords", "слова скасування")):
        a, b = set(getattr(old, attr)), set(getattr(new, attr))
        if a != b:
            lines.append(f"  {label}: + {sorted(b - a)} - {sorted(a - b)}")
    return lines
