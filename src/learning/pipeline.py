"""Retraining cycle: data -> candidate version -> check -> (approval) -> active.

It runs in batches (once a day or by hand), not online after each
post. The ETA must not change in the middle of an attack while alerts are in progress.
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
    # Do not retrain if unlabeled targets appeared in the last N hours.
    freeze_window_hours: float = 3.0
    # Share of the most recent targets that the code uses to check the new version.
    holdout_fraction: float = 0.3
    # If the check has fewer targets, automatic promotion is not allowed.
    min_holdout_targets: int = 3
    # The new version must not widen the mean range by more than this factor
    # unless it also improves the coverage.
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

    # 1. Names that a person approved.
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

    # 2. Distances, profile metadata, and speed of the jet drones.
    from .reference import seed_from_profile
    seed_ref = seed_from_profile()
    seed_points = seed_ref.points
    for pid, sp in seed_points.items():
        if pid not in cand.points:
            cand.points[pid] = PointRef(
                id=pid,
                names=list(sp.names),
                level=sp.level,
                distance_km=sp.distance_km,
                eta_min=sp.eta_min,
                eta_max=sp.eta_max,
                eta_method="prior" if sp.eta_min is not None else "none",
                is_regional=sp.is_regional,
                is_contextual=sp.is_contextual,
                name_gen=sp.name_gen,
            )
            log.append(f"Новий пункт з профілю {pid} (рівень {sp.level})")
        else:
            cp = cand.points[pid]
            if cp.distance_km is None and sp.distance_km is not None:
                cp.distance_km = sp.distance_km
            cp.is_regional = sp.is_regional
            cp.is_contextual = sp.is_contextual
            cp.name_gen = sp.name_gen
            for name in sp.names:
                norm_n = normalize(name) if not sp.is_contextual else name
                if name not in cp.names and norm_n not in index:
                    cp.names.append(name)
                    if not sp.is_contextual:
                        index[norm_n] = pid
                    log.append(f"Назва з профілю «{name}» -> {pid}")

    cand.conventional_speed_kmh = list(seed_ref.conventional_speed_kmh)
    cand.missile_speed_kmh = list(seed_ref.missile_speed_kmh)
    for kw in seed_ref.target_keywords:
        if kw not in cand.target_keywords:
            cand.target_keywords.append(kw)
            log.append(f"Ключове слово цілі з профілю: «{kw}»")
    if seed_ref.cancel_keywords and cand.cancel_keywords != seed_ref.cancel_keywords:
        added_ckw = [k for k in seed_ref.cancel_keywords if k not in cand.cancel_keywords]
        cand.cancel_keywords = list(seed_ref.cancel_keywords)
        if added_ckw:
            log.append(f"Слова скасування з профілю: +{len(added_ckw)}")
    for sw in seed_ref.stopwords:
        if sw not in cand.stopwords:
            cand.stopwords.append(sw)

    cand.post_lag_min = [0.5, 1.5]
    speed = fit_speed(observations, cand.points, cfg)
    if speed:
        # limit the speed to the range of Shahed-238 jet drones
        speed = [max(380.0, speed[0]), min(500.0, speed[1])]
    else:
        speed = [400.0, 480.0]

    if speed != cand.speed_kmh:
        log.append(f"Швидкість {cand.speed_kmh} -> {speed} км/год")
        cand.speed_kmh = speed

    # 3. ETA for each main-corridor point (skip regional and contextual points).
    durations: dict[str, list[float]] = defaultdict(list)
    for obs in observations:
        durations[obs.point_id].append(obs.minutes_to_arrival)
    for pid, point in cand.points.items():
        if point.is_regional or point.is_contextual:
            continue
        result = calibrate_point(point, durations.get(pid, []), cand.speed_kmh, cand.post_lag_min, cfg)
        if result is None:
            continue
        lo, hi, method = result
        if (lo, hi) != (point.eta_min, point.eta_max):
            log.append(f"ETA {pid}: {point.eta_min}–{point.eta_max} -> {lo:g}–{hi:g} хв ({method})")
        point.eta_min, point.eta_max, point.eta_method = lo, hi, method
        if durations.get(pid):
            point.n_obs = len(durations[pid])

    # 4. Base rates P(city | point).
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

    # Check the procedure on the most recent targets: learn from the older ones, measure on the newer ones.
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
    """Human-readable difference between two versions (for review before approval)."""
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
