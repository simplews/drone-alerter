"""Tests for the air-raid alert monitoring service (AerialAlertService)."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

import httpx

from src.siren.service import AerialAlertService, SirenState


class TestAerialAlertService(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_state_primary_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            data = {
                "source": "Vadym Klymenko API",
                "cachedat": "2026-10-11 22:00:00",
                "states": {
                    "Івано-Франківська область": {
                        "alertnow": True,
                        "changed": "2026-10-11 22:00:00",
                    },
                    "м. Київ": {"alertnow": False, "changed": "1970-01-01 03:00:00"},
                },
            }
            return httpx.Response(200, json=data)

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        service = AerialAlertService(
            region="Івано-Франківська область",
            sources=["https://primary.example.com/alerts"],
            http_client=client,
        )

        state = await service.fetch_state()
        self.assertIsNotNone(state)
        self.assertEqual(state.region, "Івано-Франківська область")
        self.assertTrue(state.is_active)
        self.assertEqual(state.changed_at, "2026-10-11 22:00:00")
        self.assertEqual(state.source_name, "Vadym Klymenko API")
        await client.aclose()

    async def test_fetch_state_failover_to_backup(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if "primary" in str(request.url):
                return httpx.Response(500, text="Internal Server Error")
            # The backup answers successfully
            data = {
                "source": "JAAM data server API",
                "states": {
                    "Івано-Франківська область": {
                        "alertnow": False,
                        "changed": "2026-10-11 21:00:00",
                    }
                },
            }
            return httpx.Response(200, json=data)

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        service = AerialAlertService(
            region="Івано-Франківська область",
            sources=[
                "https://primary.example.com/alerts",
                "https://backup.example.com/alerts",
            ],
            http_client=client,
        )

        state = await service.fetch_state()
        self.assertIsNotNone(state)
        self.assertFalse(state.is_active)
        self.assertEqual(state.source_url, "https://backup.example.com/alerts")
        self.assertEqual(call_count, 2)
        await client.aclose()

    async def test_fetch_state_all_fail_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="Bad Gateway")

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        service = AerialAlertService(
            region="Івано-Франківська область",
            sources=["https://src1.example.com", "https://src2.example.com"],
            http_client=client,
        )

        state = await service.fetch_state()
        self.assertIsNone(state)
        await client.aclose()

    async def test_state_transitions_trigger_callbacks(self):
        on_start = AsyncMock()
        on_end = AsyncMock()

        current_active = False

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "source": "Test API",
                    "states": {
                        "Івано-Франківська область": {
                            "alertnow": current_active,
                            "changed": "2026-10-11 22:10:00",
                        }
                    },
                },
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        service = AerialAlertService(
            region="Івано-Франківська область",
            sources=["https://test.example.com"],
            poll_interval_seconds=0.05,
            on_alert_start=on_start,
            on_alert_end=on_end,
            http_client=client,
        )

        task = asyncio.create_task(service.start())
        await asyncio.sleep(0.08)

        # 1. Initial state False -> no callback is called
        self.assertFalse(service.get_current_state().is_active)
        on_start.assert_not_called()
        on_end.assert_not_called()

        # 2. The alert starts (False -> True)
        current_active = True
        await asyncio.sleep(0.12)
        self.assertTrue(service.get_current_state().is_active)
        on_start.assert_called_once()
        on_end.assert_not_called()

        # 3. Next poll: the state stays True (no repeated call)
        await asyncio.sleep(0.10)
        self.assertEqual(on_start.call_count, 1)

        # 4. Alert all-clear (True -> False)
        current_active = False
        await asyncio.sleep(0.12)
        self.assertFalse(service.get_current_state().is_active)
        on_end.assert_called_once()

        await service.stop()
        await task

    async def test_bot_status_format(self):
        from aiogram.types import Chat, Message

        from src.telegram.bot import SubscriberRepository, create_bot_dispatcher

        sub_repo = MagicMock(spec=SubscriberRepository)
        siren = MagicMock()
        siren.get_current_state.return_value = SirenState(
            region="Івано-Франківська область",
            is_active=True,
            changed_at="2026-10-11 22:30:00",
            source_url="https://ubilling.net.ua/aerialalerts/",
        )

        dp = create_bot_dispatcher(sub_repo, tracker=None, siren_service=siren)
        self.assertIsNotNone(dp)

        msg = MagicMock(spec=Message)
        msg.answer = AsyncMock()
        msg.chat = MagicMock(spec=Chat, id=12345)

        status_handler = None
        for h in dp.message.handlers:
            if h.callback.__name__ == "cmd_status":
                status_handler = h.callback
                break
        self.assertIsNotNone(status_handler)
        await status_handler(msg)
        msg.answer.assert_called_once()
        args, _kwargs = msg.answer.call_args
        self.assertIn("ТРИВОГА", args[0])
        self.assertIn("Івано-Франківська область", args[0])

    async def test_siren_broadcast_locative_case(self):
        from unittest.mock import patch

        from src.main import Application

        app = Application()
        app.bot = MagicMock()

        state_start = SirenState(region="Івано-Франківська область", is_active=True)
        with patch("src.main.broadcast_siren_event", new_callable=AsyncMock) as mock_broadcast:
            await app.on_siren_start(state_start)
            mock_broadcast.assert_called_once()
            call_args = mock_broadcast.call_args[0]
            # Verify body contains "В Івано-Франківській області оголошено повітряну тривогу"
            self.assertIn("В **Івано-Франківській області** оголошено повітряну тривогу", call_args[3])

        state_end = SirenState(region="Івано-Франківська область", is_active=False)
        with patch("src.main.broadcast_siren_event", new_callable=AsyncMock) as mock_broadcast:
            await app.on_siren_end(state_end)
            mock_broadcast.assert_called_once()
            call_args = mock_broadcast.call_args[0]
            # Verify body contains locative region and siren end notice
            self.assertIn("За офіційними даними, в **Івано-Франківській області** оголошено відбій", call_args[3])


if __name__ == "__main__":
    unittest.main()
