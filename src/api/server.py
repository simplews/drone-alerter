"""FastAPI мікро-сервер для healthcheck, моніторингу активних цілей та UptimeRobot."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="IF Drone Alert API", version="1.1.0")

# Глобальні посилання на компоненти рушія (встановлюються під час запуску в main.py)
_tracker = None
_ref_cache = None
_listener = None
_start_time = datetime.now(timezone.utc)


def set_app_state(tracker=None, ref_cache=None, listener=None):
    global _tracker, _ref_cache, _listener
    _tracker = tracker
    _ref_cache = ref_cache
    _listener = listener


@app.get("/health")
def healthcheck() -> dict[str, Any]:
    """Перевірка життєздатності сервісу для UptimeRobot та Docker healthcheck."""
    uptime_seconds = (datetime.now(timezone.utc) - _start_time).total_seconds()
    active_targets_count = len(_tracker.get_active_targets()) if _tracker else 0
    ref_version = _ref_cache.get().version if _ref_cache else 0
    is_connected = _listener.is_connected() if _listener else False

    return {
        "status": "ok",
        "uptime_seconds": int(uptime_seconds),
        "active_targets_count": active_targets_count,
        "reference_version": ref_version,
        "telegram_listener_connected": is_connected,
    }


@app.get("/targets")
def get_targets() -> list[dict[str, Any]]:
    """Повертає поточні активні цілі в форматі JSON для веб-панелі."""
    if not _tracker:
        return []

    targets = _tracker.get_active_targets()
    result = []
    for t in targets:
        result.append({
            "id": t.id,
            "last_point": t.last_point_id,
            "points_sequence": t.points_sequence,
            "count": t.target_count,
            "level": t.level.value,
            "status": t.status.value,
            "confidence": t.confidence,
            "first_seen_utc": t.first_seen_utc.isoformat(),
            "last_seen_utc": t.last_seen_utc.isoformat(),
            "eta_window_start_utc": t.eta_window_start_utc.isoformat() if t.eta_window_start_utc else None,
            "eta_window_end_utc": t.eta_window_end_utc.isoformat() if t.eta_window_end_utc else None,
            "channels": list(t.channels),
        })
    return result
