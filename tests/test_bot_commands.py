"""Unit tests for Telegram bot commands and texts."""
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import Chat, Message

from src.engine.models import AlertLevel, Target
from src.telegram.bot import (
    SubscriberRepository,
    create_bot_dispatcher,
    setup_bot_commands,
)


class BotCommandsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sub_repo = MagicMock(spec=SubscriberRepository)
        self.tracker = MagicMock()
        self.tracker._get_ref.return_value = None
        self.tracker.get_active_targets.return_value = []
        self.siren_service = MagicMock()
        siren_state = MagicMock(is_active=False, region="Івано-Франківська область", changed_at="2026-10-14 18:00:00")
        self.siren_service.get_current_state.return_value = siren_state

        self.feedback_repo = MagicMock()
        self.dp = create_bot_dispatcher(
            self.sub_repo,
            tracker=self.tracker,
            siren_service=self.siren_service,
            feedback_repo=self.feedback_repo,
        )

    def _get_handler(self, name: str):
        for h in self.dp.message.handlers:
            if h.callback.__name__ == name:
                return h.callback
        raise ValueError(f"Handler {name} not found")

    async def test_cmd_start_text(self):
        handler = self._get_handler("cmd_start")
        msg = MagicMock(spec=Message, text="/start", chat=MagicMock(spec=Chat, id=100))
        msg.answer = AsyncMock()

        await handler(msg)
        self.sub_repo.add.assert_called_once_with(100)
        msg.answer.assert_called_once()
        text = msg.answer.call_args[0][0]
        self.assertIn("Вітаємо в системі завчасного попередження", text)
        self.assertIn("/status", text)
        self.assertIn("/eta", text)
        self.assertIn("/howitworks", text)
        self.assertIn("/feedback", text)
        self.assertIn("/disclaimer", text)
        self.assertIn("/test", text)
        self.assertIn("/stop", text)

    async def test_cmd_start_deeplinks(self):
        handler = self._get_handler("cmd_start")

        # /start howitworks
        msg = MagicMock(spec=Message, text="/start howitworks", chat=MagicMock(spec=Chat, id=101))
        msg.answer = AsyncMock()
        await handler(msg)
        self.assertIn("ЯК ПРАЦЮЄ СИСТЕМА", msg.answer.call_args[0][0])

        # /start eta
        msg2 = MagicMock(spec=Message, text="/start eta", chat=MagicMock(spec=Chat, id=101))
        msg2.answer = AsyncMock()
        await handler(msg2)
        self.assertIn("ТАБЛИЦЯ РУБЕЖІВ", msg2.answer.call_args[0][0])

        # /start disclaimer
        msg3 = MagicMock(spec=Message, text="/start disclaimer", chat=MagicMock(spec=Chat, id=101))
        msg3.answer = AsyncMock()
        await handler(msg3)
        self.assertIn("БЕЗПЕКОВЕ ЗАСТЕРЕЖЕННЯ", msg3.answer.call_args[0][0])

    async def test_cmd_status_idle(self):
        handler = self._get_handler("cmd_status")
        msg = MagicMock(spec=Message, text="/status", chat=MagicMock(spec=Chat, id=102))
        msg.answer = AsyncMock()

        await handler(msg)
        text = msg.answer.call_args[0][0]
        self.assertIn("Офіційна повітряна тривога:", text)
        self.assertIn("Відбій", text)
        self.assertIn("Активних цілей на коридорі не зафіксовано", text)

    async def test_cmd_status_active_target(self):
        handler = self._get_handler("cmd_status")
        now = datetime(2026, 10, 14, 18, 10, tzinfo=UTC)
        mock_target = Target(
            id="test-uuid",
            level=AlertLevel.LEVEL_1,
            last_point_id="nova_ushytsia",
            last_seen_utc=now,
            target_count=1,
            eta_min_minutes=26.0,
            eta_max_minutes=33.0,
        )
        self.tracker.get_active_targets.return_value = [mock_target]

        msg = MagicMock(spec=Message, text="/status", chat=MagicMock(spec=Chat, id=103))
        msg.answer = AsyncMock()

        await handler(msg)
        text = msg.answer.call_args[0][0]
        self.assertIn("Зафіксовано активних цілей на коридорі: 1", text)
        self.assertIn("Нова Ушиця", text)
        self.assertIn("🟡 Рівень 1 (Потенційна загроза)", text)
        self.assertIn("26–33 хв", text)

    async def test_cmd_status_regional_target(self):
        handler = self._get_handler("cmd_status")
        now = datetime(2026, 10, 15, 4, 30, tzinfo=UTC)
        mock_target = Target(
            id="regional-uuid",
            level=AlertLevel.REGIONAL,
            last_point_id="kolomyia",
            last_seen_utc=now,
            target_count=1,
            eta_min_minutes=6.0,
            eta_max_minutes=9.0,
        )
        self.tracker.get_active_targets.return_value = [mock_target]

        msg = MagicMock(spec=Message, text="/status", chat=MagicMock(spec=Chat, id=109))
        msg.answer = AsyncMock()

        await handler(msg)
        text = msg.answer.call_args[0][0]
        self.assertIn("Коломия", text)
        self.assertIn("ℹ️ Загроза в області (ймовірна тривога)", text)
        self.assertIn("загроза району області (курс не на ІФ)", text)

    async def test_cmd_eta(self):
        handler = self._get_handler("cmd_eta")
        msg = MagicMock(spec=Message, text="/eta", chat=MagicMock(spec=Chat, id=104))
        msg.answer = AsyncMock()

        await handler(msg)
        text = msg.answer.call_args[0][0]
        self.assertIn("ТАБЛИЦЯ РУБЕЖІВ ТА ОРІЄНТОВНИЙ ЧАС ПІДЛЬОТУ (ETA)", text)
        self.assertIn("Нова Ушиця", text)
        self.assertIn("Камʼянець-Подільський", text)
        self.assertIn("Рівень 1", text)
        self.assertIn("Рівень 2", text)
        self.assertIn("Рівень 3", text)

    async def test_cmd_howitworks(self):
        handler = self._get_handler("cmd_howitworks")
        msg = MagicMock(spec=Message, text="/howitworks", chat=MagicMock(spec=Chat, id=105))
        msg.answer = AsyncMock()

        await handler(msg)
        text = msg.answer.call_args[0][0]
        self.assertIn("ЯК ПРАЦЮЄ СИСТЕМА ТА ЯК МИ РАХУЄМО ЧАС", text)
        self.assertIn("@monitoring_zaxid", text)
        self.assertIn("@kpszsu", text)
        self.assertIn("@kudy_letyt", text)
        self.assertIn("@monitorwarr", text)

    async def test_cmd_disclaimer(self):
        handler = self._get_handler("cmd_disclaimer")
        msg = MagicMock(spec=Message, text="/disclaimer", chat=MagicMock(spec=Chat, id=106))
        msg.answer = AsyncMock()

        await handler(msg)
        text = msg.answer.call_args[0][0]
        self.assertIn("БЕЗПЕКОВЕ ЗАСТЕРЕЖЕННЯ (ДИСКЛЕЙМЕР)", text)
        self.assertIn("Не є офіційним джерелом", text)

    async def test_cmd_test(self):
        handler = self._get_handler("cmd_test")
        msg = MagicMock(spec=Message, text="/test", chat=MagicMock(spec=Chat, id=107))
        msg.answer = AsyncMock()

        await handler(msg)
        text = msg.answer.call_args[0][0]
        self.assertIn("[ТЕСТ] ТЕХНІЧНЕ СПОВІЩЕННЯ СИСТЕМИ", text)
        self.assertIn("Реальної загрози немає", text)

    async def test_cmd_stop(self):
        handler = self._get_handler("cmd_stop")
        msg = MagicMock(spec=Message, text="/stop", chat=MagicMock(spec=Chat, id=108))
        msg.answer = AsyncMock()

        await handler(msg)
        self.sub_repo.remove.assert_called_once_with(108)
        self.feedback_repo.delete_user_tickets.assert_called_once_with(108)
        text = msg.answer.call_args[0][0]
        self.assertIn("Всі сповіщення зупинено", text)
        self.assertIn("/start", text)

    async def test_setup_bot_commands(self):
        bot = MagicMock()
        bot.set_my_commands = AsyncMock()
        await setup_bot_commands(bot)
        bot.set_my_commands.assert_called_once()
        cmds = bot.set_my_commands.call_args[0][0]
        cmd_names = [c.command for c in cmds]
        self.assertEqual(cmd_names, ["status", "eta", "howitworks", "test", "feedback", "disclaimer", "stop"])
