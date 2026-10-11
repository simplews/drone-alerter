"""Тести для сервісу моніторингу повітряних тривог (AerialAlertService)."""
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
            # Бекап відповідає успішно
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

        # 1. Початковий стан False -> жоден колбек не викликано
        self.assertFalse(service.get_current_state().is_active)
        on_start.assert_not_called()
        on_end.assert_not_called()

        # 2. Тривога починається (False -> True)
        current_active = True
        await asyncio.sleep(0.12)
        self.assertTrue(service.get_current_state().is_active)
        on_start.assert_called_once()
        on_end.assert_not_called()

        # 3. Наступне опитування: стан залишається True (повторного виклику немає)
        await asyncio.sleep(0.10)
        self.assertEqual(on_start.call_count, 1)

        # 4. Відбій тривоги (True -> False)
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


if __name__ == "__main__":
    unittest.main()
