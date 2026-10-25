"""Unit tests for broadcast update module."""
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest

from src.telegram.bot import SubscriberRepository
from src.telegram.broadcast_update import (
    DEFAULT_UPDATE_TEXT,
    broadcast_to_subscribers,
    main,
    run_cli,
)


class BroadcastUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_subs.db")
        self.repo = SubscriberRepository(db_path=self.db_path)
        self.bot = MagicMock()
        self.bot.send_message = AsyncMock()
        self.bot.send_photo = AsyncMock()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_broadcast_to_subscribers_text(self):
        self.repo.add(1001)
        self.repo.add(1002)

        success, failed = await broadcast_to_subscribers(
            text="Hello world",
            bot=self.bot,
            subscribers=self.repo,
            throttle_delay=0.0,
        )

        self.assertEqual(success, 2)
        self.assertEqual(failed, 0)
        self.assertEqual(self.bot.send_message.call_count, 2)
        sent_chats = {call[1]["chat_id"] for call in self.bot.send_message.call_args_list}
        self.assertEqual(sent_chats, {1001, 1002})

    async def test_broadcast_to_subscribers_photo(self):
        self.repo.add(2001)

        success, failed = await broadcast_to_subscribers(
            text="Photo caption",
            bot=self.bot,
            subscribers=self.repo,
            photo_file_id="photo_123",
            throttle_delay=0.0,
        )

        self.assertEqual(success, 1)
        self.assertEqual(failed, 0)
        self.bot.send_photo.assert_called_once()
        kwargs = self.bot.send_photo.call_args[1]
        self.assertEqual(kwargs["chat_id"], 2001)
        self.assertEqual(kwargs["photo"], "photo_123")
        self.assertEqual(kwargs["caption"], "Photo caption")

    async def test_broadcast_fallback_on_parse_error(self):
        self.repo.add(3001)

        bad_req = TelegramBadRequest(method=MagicMock(), message="can't parse entities")
        self.bot.send_message.side_effect = [bad_req, MagicMock()]

        success, failed = await broadcast_to_subscribers(
            text="<invalid>markup",
            bot=self.bot,
            subscribers=self.repo,
            throttle_delay=0.0,
        )

        self.assertEqual(success, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(self.bot.send_message.call_count, 2)
        # Second call should have parse_mode=None
        self.assertIsNone(self.bot.send_message.call_args_list[1][1]["parse_mode"])

    async def test_broadcast_resilient_to_failures(self):
        self.repo.add(4001)
        self.repo.add(4002)

        # First user blocked bot, second succeeds
        self.bot.send_message.side_effect = [RuntimeError("Blocked by user"), MagicMock()]

        success, failed = await broadcast_to_subscribers(
            text="Update text",
            bot=self.bot,
            subscribers=self.repo,
            throttle_delay=0.0,
        )

        self.assertEqual(success, 1)
        self.assertEqual(failed, 1)

    async def test_run_cli_dry_run(self):
        self.repo.add(5001)
        with patch("src.telegram.broadcast_update.settings") as mock_settings:
            mock_settings.subscribers_db_path = self.db_path
            # Should not call bot
            await run_cli(text="Test", dry_run=True)
            self.bot.send_message.assert_not_called()


class BroadcastCliArgTests(unittest.TestCase):
    @patch("src.telegram.broadcast_update.run_cli")
    def test_cli_default_text(self, mock_run_cli):
        with patch("sys.argv", ["broadcast_update"]):
            main()
            mock_run_cli.assert_called_once()
            args, kwargs = mock_run_cli.call_args
            self.assertEqual(args[0], DEFAULT_UPDATE_TEXT)
            self.assertFalse(kwargs["dry_run"])
            self.assertEqual(kwargs["parse_mode"], "HTML")

    @patch("src.telegram.broadcast_update.run_cli")
    def test_cli_positional_text(self, mock_run_cli):
        with patch("sys.argv", ["broadcast_update", "Custom positional message"]):
            main()
            mock_run_cli.assert_called_once()
            args, _ = mock_run_cli.call_args
            self.assertEqual(args[0], "Custom positional message")

    @patch("src.telegram.broadcast_update.run_cli")
    def test_cli_text_flag(self, mock_run_cli):
        with patch("sys.argv", ["broadcast_update", "-t", "Flag text", "--dry-run"]):
            main()
            mock_run_cli.assert_called_once()
            args, kwargs = mock_run_cli.call_args
            self.assertEqual(args[0], "Flag text")
            self.assertTrue(kwargs["dry_run"])

    @patch("src.telegram.broadcast_update.run_cli")
    def test_cli_file_arg(self, mock_run_cli):
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as f:
            f.write("Text from temp file")
            f_path = f.name
        try:
            with patch("sys.argv", ["broadcast_update", "-f", f_path]):
                main()
                mock_run_cli.assert_called_once()
                args, _ = mock_run_cli.call_args
                self.assertEqual(args[0], "Text from temp file")
        finally:
            os.remove(f_path)
