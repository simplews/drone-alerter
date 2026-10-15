"""Unit tests for feedback repository and handlers."""
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, PhotoSize, User

from src.telegram.feedback import (
    FeedbackRepository,
    FeedbackStates,
    register_feedback_handlers,
)


class FeedbackRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_feedback.db")
        self.repo = FeedbackRepository(db_path=self.db_path)

    def tearDown(self):
        self.repo.close()
        self.temp_dir.cleanup()

    def test_create_and_get_ticket(self):
        ticket_id = self.repo.create_ticket(
            user_chat_id=12345,
            user_message_id=10,
            admin_message_id=999,
        )
        self.assertIsInstance(ticket_id, int)

        ticket = self.repo.get_ticket_by_admin_message(999)
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket["id"], ticket_id)
        self.assertEqual(ticket["user_chat_id"], 12345)
        self.assertEqual(ticket["user_message_id"], 10)
        self.assertEqual(ticket["admin_message_id"], 999)
        self.assertEqual(ticket["status"], "open")

    def test_link_additional_admin_message(self):
        ticket_id = self.repo.create_ticket(
            user_chat_id=12345,
            user_message_id=10,
            admin_message_id=999,
        )
        self.repo.link_additional_admin_message(
            ticket_id=ticket_id,
            user_chat_id=12345,
            user_message_id=10,
            admin_message_id=1000,
        )
        # Verify lookup works by the new admin message ID
        ticket = self.repo.get_ticket_by_admin_message(1000)
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket["user_chat_id"], 12345)

    def test_last_ticket_time(self):
        self.assertIsNone(self.repo.last_ticket_time(12345))
        self.repo.create_ticket(12345, 10, 999)
        t = self.repo.last_ticket_time(12345)
        self.assertIsNotNone(t)
        self.assertIsInstance(t, datetime)

    def test_delete_user_tickets(self):
        self.repo.create_ticket(12345, 10, 999)
        self.repo.create_ticket(12345, 11, 1001)
        self.repo.create_ticket(67890, 20, 1002)

        deleted = self.repo.delete_user_tickets(12345)
        self.assertEqual(deleted, 2)
        self.assertIsNone(self.repo.get_ticket_by_admin_message(999))
        self.assertIsNone(self.repo.get_ticket_by_admin_message(1001))
        self.assertIsNotNone(self.repo.get_ticket_by_admin_message(1002))


class FeedbackHandlersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_feedback.db")
        self.repo = FeedbackRepository(db_path=self.db_path)

        self.storage = MemoryStorage()
        self.dp = Dispatcher(storage=self.storage)
        self.feedback_chat_id = -1001999999999
        self.admin_id = 424242
        register_feedback_handlers(
            self.dp,
            repo=self.repo,
            feedback_chat_id=self.feedback_chat_id,
            admin_ids=[self.admin_id],
        )

        self.bot = MagicMock()
        self.bot.id = 123456
        self.bot.send_message = AsyncMock()
        self.bot.send_photo = AsyncMock()
        self.bot.copy_message = AsyncMock()

    async def asyncTearDown(self):
        self.repo.close()
        self.temp_dir.cleanup()

    def _get_fsm_context(self, chat_id: int, user_id: int) -> FSMContext:
        key = StorageKey(bot_id=self.bot.id, chat_id=chat_id, user_id=user_id)
        return FSMContext(storage=self.storage, key=key)

    async def test_cmd_feedback_initiates_flow(self):
        user_chat_id = 777
        state = self._get_fsm_context(chat_id=user_chat_id, user_id=user_chat_id)
        msg = MagicMock(
            spec=Message,
            chat=MagicMock(spec=Chat, id=user_chat_id),
            from_user=MagicMock(spec=User, id=user_chat_id),
            text="/feedback",
        )
        msg.answer = AsyncMock()

        handler = self.dp.message.handlers[0].callback
        await handler(msg, state)

        current_state = await state.get_state()
        self.assertEqual(current_state, FeedbackStates.waiting_for_feedback.state)
        msg.answer.assert_called_once()
        text = msg.answer.call_args[0][0]
        self.assertIn("Напиши, що варто покращити", text)
        self.assertIn("/cancel", text)

    async def test_cmd_feedback_rate_limit(self):
        user_chat_id = 777
        # Create ticket right now to trigger cooldown
        self.repo.create_ticket(user_chat_id, 1, 999)

        state = self._get_fsm_context(chat_id=user_chat_id, user_id=user_chat_id)
        msg = MagicMock(
            spec=Message,
            chat=MagicMock(spec=Chat, id=user_chat_id),
            from_user=MagicMock(spec=User, id=user_chat_id),
            text="/feedback",
        )
        msg.answer = AsyncMock()

        handler = self.dp.message.handlers[0].callback
        await handler(msg, state)

        # State should NOT be set
        self.assertIsNone(await state.get_state())
        msg.answer.assert_called_once()
        text = msg.answer.call_args[0][0]
        self.assertIn("Будь ласка, зачекайте", text)

    async def test_cmd_cancel(self):
        user_chat_id = 777
        state = self._get_fsm_context(chat_id=user_chat_id, user_id=user_chat_id)
        await state.set_state(FeedbackStates.waiting_for_feedback)

        msg = MagicMock(
            spec=Message,
            chat=MagicMock(spec=Chat, id=user_chat_id),
            from_user=MagicMock(spec=User, id=user_chat_id),
            text="/cancel",
        )
        msg.answer = AsyncMock()

        # Handler 1 is cmd_cancel
        handler = self.dp.message.handlers[1].callback
        await handler(msg, state)

        self.assertIsNone(await state.get_state())
        msg.answer.assert_called_once()
        self.assertIn("скасовано", msg.answer.call_args[0][0])

    async def test_process_feedback_text_success(self):
        user_chat_id = 777
        state = self._get_fsm_context(chat_id=user_chat_id, user_id=user_chat_id)
        await state.set_state(FeedbackStates.waiting_for_feedback)

        user_from = MagicMock(spec=User, id=user_chat_id, full_name="Іван Тестовий", username="ivan_test")
        msg = MagicMock(
            spec=Message,
            chat=MagicMock(spec=Chat, id=user_chat_id),
            from_user=user_from,
            text="Є помилка в розрахунку часу",
            photo=None,
            message_id=120,
        )
        msg.answer = AsyncMock()

        # Mock admin chat message response
        admin_sent_msg = MagicMock(spec=Message, message_id=888)
        self.bot.send_message.return_value = admin_sent_msg

        # Handler 2 is process_feedback_message
        handler = self.dp.message.handlers[2].callback
        await handler(msg, state, self.bot)

        # 1. State cleared
        self.assertIsNone(await state.get_state())

        # 2. Sent to admin chat
        self.bot.send_message.assert_called_once()
        admin_call_kwargs = self.bot.send_message.call_args[1]
        self.assertEqual(admin_call_kwargs["chat_id"], self.feedback_chat_id)
        self.assertIn("Іван Тестовий", admin_call_kwargs["text"])
        self.assertIn("@ivan_test", admin_call_kwargs["text"])
        self.assertIn("Є помилка в розрахунку часу", admin_call_kwargs["text"])

        # 3. Ticket stored in DB
        ticket = self.repo.get_ticket_by_admin_message(888)
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket["user_chat_id"], user_chat_id)
        self.assertEqual(ticket["user_message_id"], 120)

        # 4. Confirmation sent to user
        msg.answer.assert_called_once()
        self.assertIn("Дякуємо за відгук", msg.answer.call_args[0][0])

    async def test_process_feedback_photo_success(self):
        user_chat_id = 777
        state = self._get_fsm_context(chat_id=user_chat_id, user_id=user_chat_id)
        await state.set_state(FeedbackStates.waiting_for_feedback)

        user_from = MagicMock(spec=User, id=user_chat_id, full_name="Олена", username=None)
        photo_mock = MagicMock(spec=PhotoSize, file_id="file_abc_123")
        msg = MagicMock(
            spec=Message,
            chat=MagicMock(spec=Chat, id=user_chat_id),
            from_user=user_from,
            text=None,
            photo=[photo_mock],
            caption="Ось скриншот проблеми",
            message_id=125,
        )
        msg.answer = AsyncMock()

        admin_sent_msg = MagicMock(spec=Message, message_id=889)
        self.bot.send_photo.return_value = admin_sent_msg

        handler = self.dp.message.handlers[2].callback
        await handler(msg, state, self.bot)

        self.assertIsNone(await state.get_state())
        self.bot.send_photo.assert_called_once()
        photo_kwargs = self.bot.send_photo.call_args[1]
        self.assertEqual(photo_kwargs["chat_id"], self.feedback_chat_id)
        self.assertEqual(photo_kwargs["photo"], "file_abc_123")
        self.assertIn("Ось скриншот проблеми", photo_kwargs["caption"])

        ticket = self.repo.get_ticket_by_admin_message(889)
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket["user_chat_id"], user_chat_id)

    async def test_process_feedback_error_handling(self):
        user_chat_id = 777
        state = self._get_fsm_context(chat_id=user_chat_id, user_id=user_chat_id)
        await state.set_state(FeedbackStates.waiting_for_feedback)

        msg = MagicMock(
            spec=Message,
            chat=MagicMock(spec=Chat, id=user_chat_id),
            from_user=MagicMock(spec=User, id=user_chat_id, full_name="Тест", username=None),
            text="Тест помилки",
            photo=None,
            message_id=130,
        )
        msg.answer = AsyncMock()

        # Simulate exception during sending to admin chat
        self.bot.send_message.side_effect = RuntimeError("Telegram API failure")

        handler = self.dp.message.handlers[2].callback
        await handler(msg, state, self.bot)

        # Should NOT confirm success
        self.assertIsNone(await state.get_state())
        msg.answer.assert_called_once()
        self.assertIn("Не вдалося доставити відгук", msg.answer.call_args[0][0])
        self.assertNotIn("Дякуємо за відгук", msg.answer.call_args[0][0])

    async def test_admin_reply_to_user(self):
        user_chat_id = 777
        ticket_id = self.repo.create_ticket(
            user_chat_id=user_chat_id,
            user_message_id=120,
            admin_message_id=888,
        )

        admin_user = MagicMock(spec=User, id=self.admin_id)
        reply_to_mock = MagicMock(spec=Message, message_id=888)
        admin_reply_msg = MagicMock(
            spec=Message,
            chat=MagicMock(spec=Chat, id=self.feedback_chat_id),
            from_user=admin_user,
            text="Дякую, ми вже виправляємо!",
            photo=None,
            message_id=901,
            reply_to_message=reply_to_mock,
        )
        admin_reply_msg.reply = AsyncMock()

        # Handler 3 is handle_admin_reply
        handler = self.dp.message.handlers[3].callback
        await handler(admin_reply_msg, self.bot)

        # 1. Bot delivered anonymous response to user chat
        self.bot.send_message.assert_called_once()
        kwargs = self.bot.send_message.call_args[1]
        self.assertEqual(kwargs["chat_id"], user_chat_id)
        self.assertIn("Відповідь розробника", kwargs["text"])
        self.assertIn("Дякую, ми вже виправляємо!", kwargs["text"])

        # 2. Confirmation sent to admin
        admin_reply_msg.reply.assert_called_once()
        self.assertIn("успішно доставлено", admin_reply_msg.reply.call_args[0][0])

        # 3. Subsequent reply to message_id=901 also resolves to ticket
        ticket_followup = self.repo.get_ticket_by_admin_message(901)
        self.assertIsNotNone(ticket_followup)
        self.assertEqual(ticket_followup["ticket_id"], ticket_id)

    async def test_admin_reply_unauthorized_ignored(self):
        self.repo.create_ticket(user_chat_id=777, user_message_id=120, admin_message_id=888)

        unauthorized_user = MagicMock(spec=User, id=99999)  # Not in self.admin_ids
        admin_reply_msg = MagicMock(
            spec=Message,
            chat=MagicMock(spec=Chat, id=self.feedback_chat_id),
            from_user=unauthorized_user,
            text="Хтось чужий пише",
            message_id=902,
            reply_to_message=MagicMock(message_id=888),
        )
        admin_reply_msg.reply = AsyncMock()

        handler = self.dp.message.handlers[3].callback
        await handler(admin_reply_msg, self.bot)

        self.bot.send_message.assert_not_called()
        admin_reply_msg.reply.assert_not_called()
