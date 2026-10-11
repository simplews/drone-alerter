"""FastAPI мікро-сервер для healthcheck, моніторингу активних цілей та UptimeRobot."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

from src.engine.models import AlertEvent, AlertLevel
from src.telegram.bot import broadcast_alert, broadcast_siren_event

app = FastAPI(title="IF Drone Alert API", version="1.1.0")

# Глобальні посилання на компоненти рушія (встановлюються під час запуску в main.py)
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
    """Перевірка життєздатності сервісу для UptimeRobot та Docker healthcheck."""
    uptime_seconds = (datetime.now(UTC) - _start_time).total_seconds()
    active_targets_count = len(_tracker.get_active_targets()) if _tracker else 0
    ref_version = _ref_cache.get().version if _ref_cache else 0
    is_connected = _listener.is_connected() if _listener else False

    siren_state = _siren.get_current_state() if _siren else None

    return {
        "status": "ok",
        "uptime_seconds": int(uptime_seconds),
        "active_targets_count": active_targets_count,
        "reference_version": ref_version,
        "telegram_listener_connected": is_connected,
        "siren_alert_active": siren_state.is_active if siren_state else False,
        "siren_region": siren_state.region if siren_state else None,
        "siren_source": siren_state.source_url if siren_state else None,
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


@app.post("/test-alert")
async def trigger_test_alert(type: str = "drone") -> dict[str, Any]:
    """Надсилає тестове сповіщення всім підписникам бота (type: drone | siren | all_clear)."""
    if not _bot or not _subscribers:
        return {"status": "error", "message": "Бот або репозиторій підписників не ініціалізовано"}

    count = _subscribers.count()
    if count == 0:
        return {"status": "warning", "message": "Немає зареєстрованих підписників у базі"}

    if type == "siren":
        title = "🧪🚨🔴 **[ТЕСТ] ПОВІТРЯНА ТРИВОГА!**"
        body = (
            "Це тестова перевірка системи сповіщень про повітряну тривогу в **Івано-Франківській області**.\n\n"
            "⚠️ Реальної небезпеки немає — це перевірка звуку та доставки.\n"
            "Перевірте, чи спрацював звуковий сигнал/вібрація."
        )
        await broadcast_siren_event(_bot, _subscribers, title, body, is_silent=False)
    elif type == "all_clear":
        title = "🧪🟢 **[ТЕСТ] ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ**"
        body = (
            "Це тестова перевірка сповіщення про відбій повітряної тривоги в **Івано-Франківській області**.\n\n"
            "Тест доставки сигналу відбою."
        )
        await broadcast_siren_event(_bot, _subscribers, title, body, is_silent=False)
    else:  # "drone"
        event = AlertEvent(
            target_id="TEST-TARGET",
            level=AlertLevel.LEVEL_2,
            title="🧪🔴 [ТЕСТ] [2х] Ціль наближається (Товсте)",
            body=(
                "Ціль прямує в бік Франківщини (Товсте)!\n"
                "Очікується біля міста: 8–15 хв.\n"
                "Підготуйтеся перейти в безпечне місце.\n\n"
                "⚠️ *Час підльоту є виключно довідковим розрахунком і може змінюватися "
                "через маневри або зміну курсу. Не орієнтуйтеся на нього як на істину — переходьте в безпечні місця!*\n\n"
                "🔔 *Це тестове сповіщення системи з увімкненим звуком.*"
            ),
            is_silent=False,
            eta_window_str="8–15 хв",
        )
        await broadcast_alert(_bot, _subscribers, event)

    return {
        "status": "ok",
        "alert_type": type,
        "subscribers_notified": count,
    }
