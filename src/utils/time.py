"""Utilities for time and the Kyiv time zone (Europe/Kyiv)."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

KYIV_TZ = ZoneInfo("Europe/Kyiv")


def to_kyiv_time(dt: datetime) -> datetime:
    """Convert a datetime to the Kyiv time zone (Europe/Kyiv).

    It handles the change to summer time (UTC+3, EEST) and winter time (UTC+2, EET) automatically.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(KYIV_TZ)


def now_kyiv() -> datetime:
    """Return the current time in the Kyiv time zone."""
    return datetime.now(KYIV_TZ)


def format_kyiv_time(dt: datetime, fmt: str = "%H:%M") -> str:
    """Format a datetime object as a string in Kyiv time."""
    return to_kyiv_time(dt).strftime(fmt)
