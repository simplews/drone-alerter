"""Unit tests for target alert threading and message reply chains."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from src.engine.models import AlertEvent, AlertLevel
from src.telegram.bot import SubscriberRepository, broadcast_alert


class AlertThreadingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "subs.db")
        self.repo = SubscriberRepository(db_path=self.db_path)
        self.bot = MagicMock()
        self.bot.get_me = AsyncMock(return_value=MagicMock(username="simpledronealert_bot"))
        self.bot.send_message = AsyncMock()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    def test_subscriber_repository_target_messages(self):
        self.repo.add(101)
        self.assertIsNone(self.repo.get_target_message_id("T-1", 101))
        self.assertFalse(self.repo.has_target_alert("T-1"))

        self.repo.save_target_message_id("T-1", 101, 555)
        self.assertEqual(self.repo.get_target_message_id("T-1", 101), 555)
        self.assertTrue(self.repo.has_target_alert("T-1"))

        # Update message id
        self.repo.save_target_message_id("T-1", 101, 556)
        self.assertEqual(self.repo.get_target_message_id("T-1", 101), 556)

        # Different target
        self.assertIsNone(self.repo.get_target_message_id("T-2", 101))

        # Cleanup old messages
        cleaned = self.repo.cleanup_old_target_messages(max_age_hours=0)
        self.assertEqual(cleaned, 1)
        self.assertIsNone(self.repo.get_target_message_id("T-1", 101))

        # Removal on user /stop
        self.repo.save_target_message_id("T-1", 101, 777)
        self.repo.remove(101)
        self.assertIsNone(self.repo.get_target_message_id("T-1", 101))

    async def test_broadcast_alert_threads_replies(self):
        self.repo.add(101)
        self.repo.add(102)

        msg_101_1 = MagicMock(spec=Message, message_id=1001)
        msg_102_1 = MagicMock(spec=Message, message_id=2001)
        msg_101_2 = MagicMock(spec=Message, message_id=1002)
        msg_102_2 = MagicMock(spec=Message, message_id=2002)

        self.bot.send_message.side_effect = [msg_101_1, msg_102_1, msg_101_2, msg_102_2]

        event1 = AlertEvent(
            target_id="T-100",
            level=AlertLevel.LEVEL_1,
            title="🟡 Потенційна загроза: Нова Ушиця",
            body="Етап 1",
            is_silent=True,
        )
        await broadcast_alert(self.bot, self.repo, event1)

        # First alert sent without reply_to_message_id
        self.assertEqual(self.bot.send_message.call_count, 2)
        call_1 = self.bot.send_message.call_args_list[0]
        call_2 = self.bot.send_message.call_args_list[1]
        self.assertIsNone(call_1.kwargs.get("reply_to_message_id"))
        self.assertIsNone(call_2.kwargs.get("reply_to_message_id"))

        # Verify message IDs are stored
        self.assertEqual(self.repo.get_target_message_id("T-100", 101), 1001)
        self.assertEqual(self.repo.get_target_message_id("T-100", 102), 2001)

        # Second alert for the same target should be a reply to the first
        event2 = AlertEvent(
            target_id="T-100",
            level=AlertLevel.LEVEL_2,
            title="🟠 Наближення: Борщів",
            body="Етап 2",
            is_silent=False,
        )
        await broadcast_alert(self.bot, self.repo, event2)

        self.assertEqual(self.bot.send_message.call_count, 4)
        call_3 = self.bot.send_message.call_args_list[2]
        call_4 = self.bot.send_message.call_args_list[3]
        self.assertEqual(call_3.kwargs.get("reply_to_message_id"), 1001)
        self.assertEqual(call_4.kwargs.get("reply_to_message_id"), 2001)

        # Verify message IDs are updated to the latest
        self.assertEqual(self.repo.get_target_message_id("T-100", 101), 1002)
        self.assertEqual(self.repo.get_target_message_id("T-100", 102), 2002)

    async def test_broadcast_alert_fallback_when_replied_message_deleted(self):
        self.repo.add(101)
        self.repo.save_target_message_id("T-200", 101, 888)

        # Simulate TelegramBadRequest when replying to deleted message, then success on fallback
        fallback_msg = MagicMock(spec=Message, message_id=999)
        self.bot.send_message.side_effect = [
            TelegramBadRequest(method="send_message", message="Bad Request: message to be replied not found"),
            fallback_msg,
        ]

        event = AlertEvent(
            target_id="T-200",
            level=AlertLevel.LEVEL_2,
            title="🟠 Наближення: Борщів",
            body="Оновлення курсу",
            is_silent=False,
        )
        await broadcast_alert(self.bot, self.repo, event)

        # First call was with reply_to_message_id=888, second call was fallback without reply
        self.assertEqual(self.bot.send_message.call_count, 2)
        self.assertEqual(self.bot.send_message.call_args_list[0].kwargs.get("reply_to_message_id"), 888)
        self.assertIsNone(self.bot.send_message.call_args_list[1].kwargs.get("reply_to_message_id"))

        # Updated to the new message ID
        self.assertEqual(self.repo.get_target_message_id("T-200", 101), 999)
