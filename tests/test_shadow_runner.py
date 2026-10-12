import json
from datetime import UTC, datetime
from pathlib import Path

from src.engine.shadow_runner import (
    _extract_text,
    format_scorecard_report,
    parse_posts_from_json,
    run_historical_replay,
)
from src.engine.simulation import EvaluationMetrics
from src.learning.reference import (
    ActiveReferenceCache,
    ReferenceRepository,
    seed_from_profile,
)


def test_extract_text():
    assert _extract_text("hello") == "hello"
    assert _extract_text(["hello ", {"text": "world"}]) == "hello world"
    assert _extract_text(123) == "123"


def test_parse_posts_and_replay(tmp_path: Path):
    # Test both a plain message list and the Telegram export format
    tg_export = {
        "name": "monitoring_test",
        "type": "public_channel",
        "messages": [
            {
                "id": 1,
                "type": "message",
                "date": "2026-03-24T04:00:00",
                "text": "Стара Ушиця -> реактивна герань курсом на захід",
            },
            {
                "id": 2,
                "type": "message",
                "date": "2026-03-24T04:05:00",
                "text": "Камʼянець-Подільський, курс західний",
            },
            {
                "id": 3,
                "type": "service",
                "date": "2026-03-24T04:06:00",
                "text": "pin message",
            },
        ],
    }

    file_path = tmp_path / "result.json"
    file_path.write_text(json.dumps(tg_export), encoding="utf-8")

    posts = parse_posts_from_json(file_path)
    assert len(posts) == 2
    assert posts[0].channel == "monitoring_test"
    assert posts[0].message_id == 1
    assert posts[0].posted_at_utc == datetime(2026, 3, 24, 4, 0, tzinfo=UTC)

    # Replay
    repo = ReferenceRepository(tmp_path / "reference")
    v1 = repo.save_new(seed_from_profile())
    repo.promote(v1.version)
    cache = ActiveReferenceCache(repo)

    alerts, tracker = run_historical_replay(posts, cache)
    assert len(alerts) > 0
    assert len(tracker._active_targets) == 1


def test_format_scorecard_report():
    metrics = EvaluationMetrics(
        total_episodes=5,
        total_targets=5,
        level_2_alerts_count=4,
        level_3_alerts_count=3,
        eta_within_range_count=4,
        eta_evaluable_count=5,
        eta_coverage_percent=80.0,
        lead_time_over_official_minutes=[8.0, 9.0, 10.0],
        lead_time_over_manual_minutes=[6.0, 7.0],
        false_level_2_rate_percent=10.0,
        false_closures_count=0,
        missed_targets_count=0,
        go_no_go_checklist={
            "ETA coverage >= 80%": True,
            "Lead time over official alert >= 7 min": True,
            "Lead time over manual channels >= 5 min": True,
            "False level 2 alerts <= 25%": True,
            "False closures of active targets == 0%": True,
            "Missed targets <= 5%": True,
        },
    )
    report = format_scorecard_report(metrics)
    assert "SHADOW MODE EVALUATION SCORECARD" in report
    assert "ГОТОВО ДО БЕТА-РЕЛІЗУ" in report
