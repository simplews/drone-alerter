"""Async listener for the monitoring Telegram channels through Telethon (MTProto)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime

from telethon import TelegramClient, errors, events
from telethon.sessions import StringSession

from src.engine.models import RawPost

logger = logging.getLogger(__name__)


class TelegramChannelListener:
    """Connect to Telegram through MTProto and relay the channel events in real time."""

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_str: str,
        channels: list[str],
        on_post: Callable[[RawPost], Coroutine],
        on_connection_lost: Callable[[], Coroutine] | None = None,
        on_connection_restored: Callable[[], Coroutine] | None = None,
        is_outage: Callable[[], bool] | None = None,
        set_outage: Callable[[bool], None] | None = None,
        heartbeat_interval: float = 15.0,
    ):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_str = session_str
        self.channels = channels
        self.on_post = on_post
        self.on_connection_lost = on_connection_lost
        self.on_connection_restored = on_connection_restored
        self.is_outage = is_outage
        self.set_outage = set_outage
        self.heartbeat_interval = heartbeat_interval

        self.client: TelegramClient | None = None
        self._is_running = False
        self._handlers_registered = False
        self._last_event_utc: datetime | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._outage_active = bool(is_outage()) if is_outage else False

    def is_connected(self) -> bool:
        return bool(self.client and self.client.is_connected())

    def last_event_time(self) -> datetime | None:
        return self._last_event_utc

    def _register_handlers(self) -> None:
        if not self.client or self._handlers_registered:
            return

        # Handler for new messages
        @self.client.on(events.NewMessage(chats=self.channels))
        async def handle_new_message(event):
            await self._process_telethon_event(event, is_edit=False)

        # Handler for edited messages (requirement F4)
        @self.client.on(events.MessageEdited(chats=self.channels))
        async def handle_edited_message(event):
            await self._process_telethon_event(event, is_edit=True)

        self._handlers_registered = True

    async def _connect_and_subscribe(self) -> bool:
        if not self.client:
            return False
        try:
            if not self.client.is_connected():
                await self.client.connect()
            if not await self.client.is_user_authorized():
                logger.error(
                    "Клієнт Telethon не авторизований! Для роботи без вводу коду збережіть "
                    "StringSession у змінну середовища TELETHON_STRING_SESSION."
                )
                return False
            self._register_handlers()
            return True
        except errors.AuthKeyDuplicatedError:
            logger.critical(
                "❌ Помилка Telethon AuthKeyDuplicatedError: сесія була використана з двох різних IP-адрес одночасно "
                "і анульована Telegram. Потрібно згенерувати нову сесію: python -m src.telegram.generate_session "
                "і оновити TELETHON_STRING_SESSION в .env."
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Помилка підключення до Telegram MTProto: {exc}")
            return False

    async def start(self) -> None:
        """Initialize and start the Telethon client."""
        if not self.api_id or not self.api_hash:
            logger.warning("TELETHON_API_ID або TELETHON_API_HASH не налаштовані. Слухач не запущено.")
            return

        session = StringSession(self.session_str) if self.session_str else StringSession("")
        self.client = TelegramClient(session, self.api_id, self.api_hash)
        self._is_running = True

        connected = await self._connect_and_subscribe()
        if connected:
            logger.info(f"Telethon підключено. Підписка на канали: {self.channels}")
            if self._outage_active:
                logger.info("З'єднання з Telegram відновлено після попереднього збою/перезапуску.")
                self._outage_active = False
                if self.set_outage:
                    self.set_outage(False)
                if self.on_connection_restored:
                    await self.on_connection_restored()
        else:
            logger.warning("Не вдалося підключитися до Telegram при старті. Слухач переходить у фоновий режим підключення.")
            if not self._outage_active:
                self._outage_active = True
                if self.set_outage:
                    self.set_outage(True)
                if self.on_connection_lost:
                    await self.on_connection_lost()

        # Start the background connection liveness monitor (heartbeat)
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _process_telethon_event(self, event, is_edit: bool) -> None:
        try:
            chat = await event.get_chat()
            channel_name = getattr(chat, "username", None) or str(chat.id)
            text = event.raw_text or ""
            posted_at = event.message.date.astimezone(UTC)
            reply_to_msg_id = getattr(event.message, "reply_to_msg_id", None)
            self._last_event_utc = datetime.now(UTC)

            post = RawPost(
                channel=channel_name,
                message_id=event.id,
                text=text,
                posted_at_utc=posted_at,
                is_edit=is_edit,
                reply_to_message_id=reply_to_msg_id,
            )
            await self.on_post(post)
        except Exception:
            logger.exception("Помилка при обробці події Telegram")

    async def _heartbeat_loop(self) -> None:
        """Check the socket liveness periodically and reconnect if dropped."""
        while self._is_running:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                connected = self.is_connected()

                if not connected:
                    if not self._outage_active:
                        self._outage_active = True
                        logger.warning("Втрачено з'єднання з Telegram MTProto!")
                        if self.set_outage:
                            self.set_outage(True)
                        if self.on_connection_lost:
                            await self.on_connection_lost()

                    # Attempt reconnection
                    logger.info("Спроба відновити з'єднання з Telegram MTProto...")
                    reconnected = await self._connect_and_subscribe()
                    if reconnected:
                        self._outage_active = False
                        logger.info("З'єднання з Telegram MTProto успішно відновлено.")
                        if self.set_outage:
                            self.set_outage(False)
                        if self.on_connection_restored:
                            await self.on_connection_restored()

                else:
                    # connected is True
                    if self._outage_active:
                        self._outage_active = False
                        logger.info("З'єднання з Telegram MTProto відновлено.")
                        if self.set_outage:
                            self.set_outage(False)
                        if self.on_connection_restored:
                            await self.on_connection_restored()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Помилка в циклі heartbeat: {exc}")

    async def stop(self) -> None:
        self._is_running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self.client:
            await self.client.disconnect()
        logger.info("Слухач Telegram зупинено.")
