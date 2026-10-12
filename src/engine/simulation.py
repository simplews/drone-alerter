"""Module for simulation and metric evaluation in shadow mode (Shadow Mode Evaluation).

It runs a batch check of scenarios. It calculates how well the results meet
the Go/No-Go criteria from section 7 of PRD v1.1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.engine.models import AlertEvent, AlertLevel, RawPost
from src.engine.tracker import TargetTracker
from src.learning.reference import ActiveReferenceCache
from src.nlp.normalizer import MessageNormalizer


@dataclass
class SimulationEpisode:
    episode_id: str
    description: str
    posts: list[RawPost]
    actual_arrival_utc: datetime | None = None  # actual arrival time (if the target arrived)
    official_alert_utc: datetime | None = None  # time when the official danger was declared for the district
    manual_channel_mention_utc: datetime | None = None  # first mention of IF in the monitoring channels


@dataclass
class EvaluationMetrics:
    total_episodes: int = 0
    total_targets: int = 0
    level_2_alerts_count: int = 0
    level_3_alerts_count: int = 0
    
    # ETA accuracy metrics
    eta_within_range_count: int = 0
    eta_evaluable_count: int = 0
    eta_coverage_percent: float = 0.0
    
    # Lead time metrics
    lead_time_over_official_minutes: list[float] = field(default_factory=list)
    lead_time_over_manual_minutes: list[float] = field(default_factory=list)
    
    # Safety metrics
    false_level_2_count: int = 0
    false_level_2_rate_percent: float = 0.0
    false_closures_count: int = 0
    missed_targets_count: int = 0
    
    # Go/No-Go status
    go_no_go_checklist: dict[str, bool] = field(default_factory=dict)


class ShadowModeSimulator:
    def __init__(self, ref_cache: ActiveReferenceCache):
        self.ref_cache = ref_cache

    def run_simulation(self, episodes: list[SimulationEpisode]) -> tuple[EvaluationMetrics, list[dict[str, Any]]]:
        metrics = EvaluationMetrics(total_episodes=len(episodes))
        detailed_logs = []

        for episode in episodes:
            tracker = TargetTracker(self.ref_cache)
            normalizer = MessageNormalizer(self.ref_cache)
            episode_alerts: list[AlertEvent] = []

            first_level_2_time: datetime | None = None

            for post in sorted(episode.posts, key=lambda p: p.posted_at_utc):
                parsed = normalizer.parse(post)
                alerts = tracker.process_message(post, parsed, now=post.posted_at_utc)
                for alert in alerts:
                    episode_alerts.append(alert)
                    if alert.level == AlertLevel.LEVEL_2 and first_level_2_time is None:
                        first_level_2_time = post.posted_at_utc

            targets = list(tracker._active_targets.values())
            metrics.total_targets += len(targets)

            # Check the lead time
            if first_level_2_time:
                metrics.level_2_alerts_count += 1
                if episode.official_alert_utc:
                    lead_official = (episode.official_alert_utc - first_level_2_time).total_seconds() / 60.0
                    metrics.lead_time_over_official_minutes.append(lead_official)
                if episode.manual_channel_mention_utc:
                    lead_manual = (episode.manual_channel_mention_utc - first_level_2_time).total_seconds() / 60.0
                    metrics.lead_time_over_manual_minutes.append(lead_manual)

            # Check the ETA coverage
            if episode.actual_arrival_utc and targets:
                target = targets[0]
                metrics.eta_evaluable_count += 1
                if target.eta_window_start_utc and target.eta_window_end_utc:
                    hit = (target.eta_window_start_utc <= episode.actual_arrival_utc <= target.eta_window_end_utc)
                    if hit:
                        metrics.eta_within_range_count += 1

            detailed_logs.append({
                "episode_id": episode.episode_id,
                "description": episode.description,
                "alerts_count": len(episode_alerts),
                "first_level_2_time": first_level_2_time.isoformat() if first_level_2_time else None,
                "targets_count": len(targets),
            })

        # Calculate the percentages
        if metrics.eta_evaluable_count > 0:
            metrics.eta_coverage_percent = round(
                (metrics.eta_within_range_count / metrics.eta_evaluable_count) * 100.0, 1
            )
        
        # Go/No-Go criteria from PRD section 7
        med_lead_official = (
            sorted(metrics.lead_time_over_official_minutes)[len(metrics.lead_time_over_official_minutes) // 2]
            if metrics.lead_time_over_official_minutes else 0.0
        )
        med_lead_manual = (
            sorted(metrics.lead_time_over_manual_minutes)[len(metrics.lead_time_over_manual_minutes) // 2]
            if metrics.lead_time_over_manual_minutes else 0.0
        )

        metrics.go_no_go_checklist = {
            "ETA coverage >= 80%": metrics.eta_coverage_percent >= 80.0,
            "Lead time over official alert >= 7 min": med_lead_official >= 7.0,
            "Lead time over manual channels >= 5 min": med_lead_manual >= 5.0,
            "False level 2 alerts <= 25%": metrics.false_level_2_rate_percent <= 25.0,
            "False closures of active targets == 0%": metrics.false_closures_count == 0,
            "Missed targets <= 5%": metrics.missed_targets_count == 0,
        }

        return metrics, detailed_logs
