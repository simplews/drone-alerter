"""Unit tests for TelegramChannelListener connection lifecycle, outage persistence, and alerts."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.telegram.bot import SubscriberRepository
from src.telegram.listener import TelegramChannelListener


class TestSubscriberRepositoryState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test_subscribers.db")
        self.repo = SubscriberRepository(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_and_set_state(self):
        self.assertIsNone(self.repo.get_state("non_existent"))
        self.assertEqual(self.repo.get_state("non_existent", "default_val"), "default_val")

        self.repo.set_state("listener_outage", "1")
        self.assertEqual(self.repo.get_state("listener_outage"), "1")

        self.repo.set_state("listener_outage", "0")
        self.assertEqual(self.repo.get_state("listener_outage"), "0")

    def test_listener_outage_defaults_to_1_if_subscribers_exist_and_no_state(self):
        # Fresh db with 0 subscribers
        self.assertIsNone(self.repo.get_state("listener_outage"))

        # Add a subscriber (representing pre-existing production database)
        self.repo.add(12345678)
        self.assertEqual(self.repo.count(), 1)

        # Now listener_outage should default to "1" so restoration notification fires on start
        self.assertEqual(self.repo.get_state("listener_outage"), "1")

        # Once explicitly cleared, it stays "0"
        self.repo.set_state("listener_outage", "0")
        self.assertEqual(self.repo.get_state("listener_outage"), "0")


class TestTelegramChannelListenerLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.on_post = AsyncMock()
        self.on_lost = AsyncMock()
        self.on_restored = AsyncMock()
        self.outage_val = False

        def is_outage():
            return self.outage_val

        def set_outage(val: bool):
            self.outage_val = val

        self.is_outage = is_outage
        self.set_outage = set_outage

    @patch("src.telegram.listener.TelegramClient")
    async def test_startup_clean_no_outage(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_client.is_connected = MagicMock(return_value=True)
        mock_client_cls.return_value = mock_client

        listener = TelegramChannelListener(
            api_id=123,
            api_hash="hash",
            session_str="",
            channels=["chan"],
            on_post=self.on_post,
            on_connection_lost=self.on_lost,
            on_connection_restored=self.on_restored,
            is_outage=self.is_outage,
            set_outage=self.set_outage,
            heartbeat_interval=0.01,
        )

        await listener.start()
        # Clean start: no restored alert should be broadcast
        self.on_restored.assert_not_called()
        self.on_lost.assert_not_called()
        self.assertFalse(self.outage_val)
        self.assertTrue(listener.is_connected())

        await listener.stop()

    @patch("src.telegram.listener.TelegramClient")
    async def test_startup_after_outage_triggers_restoration(self, mock_client_cls):
        self.outage_val = True  # Pre-existing outage from previous run

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_client.is_connected = MagicMock(return_value=True)
        mock_client_cls.return_value = mock_client

        listener = TelegramChannelListener(
            api_id=123,
            api_hash="hash",
            session_str="",
            channels=["chan"],
            on_post=self.on_post,
            on_connection_lost=self.on_lost,
            on_connection_restored=self.on_restored,
            is_outage=self.is_outage,
            set_outage=self.set_outage,
            heartbeat_interval=0.01,
        )

        await listener.start()
        # Outage was active: on_connection_restored MUST be called
        self.on_restored.assert_called_once()
        self.on_lost.assert_not_called()
        self.assertFalse(self.outage_val)  # Outage cleared

        await listener.stop()

    @patch("src.telegram.listener.TelegramClient")
    async def test_startup_failure_triggers_outage(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.connect = AsyncMock(side_effect=Exception("Network error"))
        mock_client.disconnect = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        listener = TelegramChannelListener(
            api_id=123,
            api_hash="hash",
            session_str="",
            channels=["chan"],
            on_post=self.on_post,
            on_connection_lost=self.on_lost,
            on_connection_restored=self.on_restored,
            is_outage=self.is_outage,
            set_outage=self.set_outage,
            heartbeat_interval=0.01,
        )

        await listener.start()
        # Connection failed on startup: on_connection_lost MUST be called
        self.on_lost.assert_called_once()
        self.on_restored.assert_not_called()
        self.assertTrue(self.outage_val)

        await listener.stop()

    @patch("src.telegram.listener.TelegramClient")
    async def test_heartbeat_disconnect_and_reconnect(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_client.is_connected = MagicMock(return_value=True)
        mock_client_cls.return_value = mock_client

        listener = TelegramChannelListener(
            api_id=123,
            api_hash="hash",
            session_str="",
            channels=["chan"],
            on_post=self.on_post,
            on_connection_lost=self.on_lost,
            on_connection_restored=self.on_restored,
            is_outage=self.is_outage,
            set_outage=self.set_outage,
            heartbeat_interval=0.02,
        )

        await listener.start()
        self.on_lost.assert_not_called()
        self.on_restored.assert_not_called()

        # Simulate disconnect
        mock_client.is_connected.return_value = False
        mock_client.connect = AsyncMock(side_effect=Exception("Still down"))

        # Wait for heartbeat loop to tick
        await asyncio.sleep(0.06)
        self.on_lost.assert_called_once()
        self.assertTrue(self.outage_val)

        # Ensure subsequent ticks while down do not repeat on_lost
        await asyncio.sleep(0.05)
        self.assertEqual(self.on_lost.call_count, 1)

        # Now simulate reconnect
        mock_client.connect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_client.is_connected.return_value = True

        await asyncio.sleep(0.06)
        self.on_restored.assert_called_once()
        self.assertFalse(self.outage_val)

        await listener.stop()
