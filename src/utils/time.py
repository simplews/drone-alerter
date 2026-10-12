"""Утиліти для роботи з часом та часовим поясом Києва (Europe/Kyiv)."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

KYIV_TZ = ZoneInfo("Europe/Kyiv")


def to_kyiv_time(dt: datetime) -> datetime:
    """Конвертує datetime у київський часовий пояс (Europe/Kyiv).

    Автоматично враховує перехід на літній час (UTC+3, EEST) та зимовий час (UTC+2, EET).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(KYIV_TZ)


def now_kyiv() -> datetime:
    """Повертає поточний час у часовому поясі Києва."""
    return datetime.now(KYIV_TZ)


def format_kyiv_time(dt: datetime, fmt: str = "%H:%M") -> str:
    """Форматує об'єкт datetime у рядок за київським часом."""
    return to_kyiv_time(dt).strftime(fmt)
