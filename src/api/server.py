"""FastAPI micro-server for the healthcheck, active-target monitoring and UptimeRobot."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

from src.profile import get_profile
from src.telegram.bot import broadcast_siren_event
from src.utils.time import now_kyiv

app = FastAPI(title=f"{get_profile().short_name} Drone Alert API", version="1.1.0")

# Global references to the engine components (main.py sets them at startup)
_tracker = None
_ref_cache = None
_listener = None
_siren = None
_bot = None
_subscribers = None
_start_time = datetime.now(UTC)


def set_app_state(
    tracker=None,
    ref_cache=None,
    listener=None,
    siren_service=None,
    bot=None,
    subscribers=None,
):
    global _tracker, _ref_cache, _listener, _siren, _bot, _subscribers
    _tracker = tracker
    _ref_cache = ref_cache
    _listener = listener
    _siren = siren_service
    _bot = bot
    _subscribers = subscribers


@app.get("/health")
def healthcheck() -> dict[str, Any]:
    """Check that the service is alive (for UptimeRobot and the Docker healthcheck)."""
    uptime_seconds = (datetime.now(UTC) - _start_time).total_seconds()
    active_targets_count = len(_tracker.get_active_targets()) if _tracker else 0
    ref_version = _ref_cache.get().version if _ref_cache else 0
    is_connected = _listener.is_connected() if _listener else False
    subscribers_count = _subscribers.count() if _subscribers else 0

    siren_state = _siren.get_current_state() if _siren else None

    return {
        "status": "ok",
        "uptime_seconds": int(uptime_seconds),
        "active_targets_count": active_targets_count,
        "subscribers_count": subscribers_count,
        "reference_version": ref_version,
        "telegram_listener_connected": is_connected,
        "siren_alert_active": siren_state.is_active if siren_state else False,
        "siren_region": siren_state.region if siren_state else None,
        "siren_source": siren_state.source_url if siren_state else None,
    }


@app.get("/targets")
def get_targets() -> list[dict[str, Any]]:
    """Return the current active targets as JSON for the web dashboard."""
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


@app.post("/test-alert")
async def trigger_test_alert(type: str = "drone") -> dict[str, Any]:
    """Send a test alert to all bot subscribers (type: drone | siren | all_clear)."""
    if not _bot or not _subscribers:
        return {"status": "error", "message": "Бот або репозиторій підписників не ініціалізовано"}

    count = _subscribers.count()
    if count == 0:
        return {"status": "warning", "message": "Немає зареєстрованих підписників у базі"}

    now_k = now_kyiv()
    time_str = now_k.strftime("%H:%M")

    if type == "siren":
        title = "🧪🚨🔴 **[ТЕСТ] ПОВІТРЯНА ТРИВОГА!**"
        body = (
            f"Це тестова перевірка системи сповіщень про повітряну тривогу в **{get_profile().texts['region_loc']}** (о {time_str}).\n\n"
            "⚠️ Реальної небезпеки немає — це перевірка звуку та доставки.\n"
            "Перевірте, чи спрацював звуковий сигнал/вібрація."
        )
        await broadcast_siren_event(_bot, _subscribers, title, body, is_silent=False)
    elif type == "all_clear":
        title = "🧪🟢 **[ТЕСТ] ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ**"
        body = (
            f"Це тестова перевірка сповіщення про відбій повітряної тривоги в **{get_profile().texts['region_loc']}** (о {time_str}).\n\n"
            "Тест доставки сигналу відбою."
        )
        await broadcast_siren_event(_bot, _subscribers, title, body, is_silent=False)
    else:  # "drone"
        title = "🧪 **[ТЕСТ] ТЕХНІЧНЕ СПОВІЩЕННЯ СИСТЕМИ**"
        body = (
            "Це тестова перевірка доставки звуку та налаштувань сповіщень.\n"
            "⚠️ **Реальної загрози немає!**\n\n"
            "Дякуємо, що ви з нами! Бажаємо тихого і безпечного дня! 🕊"
        )
        await broadcast_siren_event(_bot, _subscribers, title, body, is_silent=False)



    return {
        "status": "ok",
        "alert_type": type,
        "subscribers_notified": count,
    }
