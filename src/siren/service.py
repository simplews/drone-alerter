"""Service to monitor official air-raid alerts through the UBilling Aerial Alerts API, with backup sources."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

logger = logging.getLogger(__name__)

DEFAULT_SIREN_SOURCES: list[str] = [
    "https://ubilling.net.ua/aerialalerts/",
    "https://ubilling.net.ua/aerialalerts/?source=jaam",
    "https://ubilling.net.ua/aerialalerts/?source=skog",
    "https://ubilling.net.ua/aerialalerts/?source=klimenko",
    "http://ubilling.net.ua/aerialalerts/",
]


@dataclass
class SirenState:
    region: str
    is_active: bool
    changed_at: str | None = None
    updated_at_utc: datetime | None = None
    source_url: str = ""
    source_name: str = ""


class AerialAlertService:
    """Poll the air-raid alert API and report a change of the alert status for the given region."""

    def __init__(
        self,
        region: str = "Івано-Франківська область",
        sources: list[str] | None = None,
        poll_interval_seconds: float = 12.0,
        request_timeout_seconds: float = 5.0,
        on_alert_start: Callable[[SirenState], Awaitable[None]] | None = None,
        on_alert_end: Callable[[SirenState], Awaitable[None]] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.region = region
        self.sources = list(sources) if sources else list(DEFAULT_SIREN_SOURCES)
        self.poll_interval_seconds = poll_interval_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.on_alert_start = on_alert_start
        self.on_alert_end = on_alert_end

        self._custom_client = http_client
        self._current_state: SirenState | None = None
        self._stop_event = asyncio.Event()
        self._last_successful_source: str | None = None

    def get_current_state(self) -> SirenState | None:
        """Return the current recorded alert state."""
        return self._current_state

    async def fetch_state(self, client: httpx.AsyncClient | None = None) -> SirenState | None:
        """Poll the sources in turn. Switch to the backup automatically on errors."""
        owns_client = False
        if client is None:
            if self._custom_client:
                client = self._custom_client
            else:
                client = httpx.AsyncClient(
                    timeout=self.request_timeout_seconds,
                    headers={"User-Agent": "IF-Drone-Alert/1.1 (AerialAlertsMonitor)"},
                )
                owns_client = True

        try:
            # Try the last successful source first (if there was one), then the others
            ordered_sources = list(self.sources)
            if self._last_successful_source and self._last_successful_source in ordered_sources:
                ordered_sources.remove(self._last_successful_source)
                ordered_sources.insert(0, self._last_successful_source)

            for source_url in ordered_sources:
                try:
                    resp = await client.get(source_url)
                    if resp.status_code != 200:
                        logger.warning(
                            f"Джерело тривог {source_url} повернуло статус {resp.status_code}. Пробуємо наступне..."
                        )
                        continue

                    data = resp.json()
                    states = data.get("states")
                    if not isinstance(states, dict):
                        logger.warning(f"Невірний формат JSON у {source_url}: відсутнє поле 'states'")
                        continue

                    region_data = states.get(self.region)
                    if region_data is None:
                        logger.warning(f"Регіон '{self.region}' не знайдено у відповіді {source_url}")
                        continue

                    is_active = bool(region_data.get("alertnow", False))
                    changed_at = region_data.get("changed")
                    source_name = str(data.get("source", source_url))

                    self._last_successful_source = source_url
                    return SirenState(
                        region=self.region,
                        is_active=is_active,
                        changed_at=changed_at,
                        updated_at_utc=datetime.now(UTC),
                        source_url=source_url,
                        source_name=source_name,
                    )

                except (httpx.RequestError, ValueError, KeyError) as exc:
                    logger.warning(f"Помилка опитування джерела {source_url}: {exc}. Пробуємо бекап...")

            logger.error("Усі джерела повітряних тривог тимчасово недоступні!")
            return None

        finally:
            if owns_client:
                await client.aclose()

    async def start(self) -> None:
        """Main loop of the background alert monitoring."""
        self._stop_event.clear()
        logger.info(f"Запуск моніторингу повітряних тривог для '{self.region}'...")

        # First poll without a spam alert (set the baseline)
        initial = await self.fetch_state()
        if initial:
            self._current_state = initial
            status_text = "🔴 ТРИВОГА" if initial.is_active else "🟢 ВІДБІЙ"
            logger.info(f"Початковий статус тривоги ({self.region}): {status_text} (джерело: {initial.source_url})")

        while not self._stop_event.is_set():
            try:
                # Wait for the interval before the next poll
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval_seconds)
                    break  # If stop_event fired
                except TimeoutError:
                    pass

                state = await self.fetch_state()
                if state is None:
                    # All sources failed: keep the previous state, do not create false alerts or all-clears
                    continue

                prev_active = self._current_state.is_active if self._current_state else None
                self._current_state = state

                # Check for a status change
                if prev_active is not None and prev_active != state.is_active:
                    if state.is_active:
                        logger.warning(f"🚨 ОГОЛОШЕНО ПОВІТРЯНУ ТРИВОГУ: {self.region}!")
                        if self.on_alert_start:
                            await self.on_alert_start(state)
                    else:
                        logger.info(f"🟢 ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ: {self.region}!")
                        if self.on_alert_end:
                            await self.on_alert_end(state)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Непередбачена помилка монітора тривог")

        logger.info("Моніторинг повітряних тривог зупинено.")

    async def stop(self) -> None:
        """Stop the background monitoring."""
        self._stop_event.set()
        if self._custom_client:
            await self._custom_client.aclose()
