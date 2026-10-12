"""Main entry point: coordinates the async listener, the target analyzer, the bot and the API."""
from __future__ import annotations

import asyncio
import logging
import signal

import uvicorn
from aiogram import Bot

from src.api.server import app as api_app
from src.api.server import set_app_state
from src.config import settings
from src.engine.models import RawPost
from src.engine.tracker import TargetTracker
from src.learning.reference import (
    ActiveReferenceCache,
    ReferenceRepository,
    seed_from_profile,
)
from src.learning.store import LearningStore
from src.nlp.normalizer import MessageNormalizer
from src.siren import AerialAlertService, SirenState
from src.telegram.bot import (
    SubscriberRepository,
    broadcast_alert,
    broadcast_siren_event,
    broadcast_system_warning,
    create_bot_dispatcher,
)
from src.telegram.listener import TelegramChannelListener
from src.utils.time import now_kyiv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("drone_alert_main")


class Application:
    def __init__(self):
        self.stop_event = asyncio.Event()

        # 1. References and storages
        self.repo = ReferenceRepository(settings.reference_dir)
        if not self.repo.versions():
            logger.info("Ініціалізація початкового довідника (seed v0001)...")
            v1 = self.repo.save_new(seed_from_profile())
            self.repo.promote(v1.version)

        self.ref_cache = ActiveReferenceCache(self.repo)
        self.learning_store = LearningStore(settings.learning_db_path)
        self.subscriber_repo = SubscriberRepository(settings.subscribers_db_path)

        # 2. Domain core
        self.normalizer = MessageNormalizer(self.ref_cache)
        self.tracker = TargetTracker(self.ref_cache, store=self.learning_store)

        # 3. Official air-raid alert service
        self.siren_service: AerialAlertService | None = None
        if settings.siren_enabled:
            self.siren_service = AerialAlertService(
                region=settings.siren_region or None,
                sources=settings.siren_sources,
                poll_interval_seconds=settings.siren_poll_interval_seconds,
                on_alert_start=self.on_siren_start,
                on_alert_end=self.on_siren_end,
            )

        # 4. Telegram adapters
        self.bot: Bot | None = None
        self.bot_dp = None
        if settings.telegram_bot_token:
            self.bot = Bot(token=settings.telegram_bot_token)
            self.bot_dp = create_bot_dispatcher(self.subscriber_repo, self.tracker, self.siren_service)
        else:
            logger.warning("TELEGRAM_BOT_TOKEN не задано. Бот сповіщень вимкнено.")

        self.listener: TelegramChannelListener | None = None
        if settings.telethon_api_id and settings.telethon_api_hash:
            self.listener = TelegramChannelListener(
                api_id=settings.telethon_api_id,
                api_hash=settings.telethon_api_hash,
                session_str=settings.telethon_string_session,
                channels=settings.monitored_channels,
                on_post=self.on_telegram_post,
                on_connection_lost=self.on_connection_lost,
                on_connection_restored=self.on_connection_restored,
            )
        else:
            logger.warning("TELETHON_API_ID / HASH не задано. Читання каналів вимкнено.")

        # Register the components in FastAPI
        set_app_state(
            tracker=self.tracker,
            ref_cache=self.ref_cache,
            listener=self.listener,
            siren_service=self.siren_service,
            bot=self.bot,
            subscribers=self.subscriber_repo,
        )

    async def on_telegram_post(self, post: RawPost) -> None:
        """Process a post that arrives from a channel in real time."""
        parsed = self.normalizer.parse(post)
        alerts = self.tracker.process_message(post, parsed)

        for alert in alerts:
            if alert.is_update:
                logger.debug(f"ℹ️ [ПОВТОР/ОНОВЛЕННЯ] {alert.title} (дедупліковано в межах вікна)")
                continue
            logger.info(f"🚨 [СПОЗВІЩЕННЯ] {alert.title} (рівень={alert.level.value}, silent={alert.is_silent})")
            if self.bot:
                await broadcast_alert(self.bot, self.subscriber_repo, alert)

    async def on_connection_lost(self) -> None:
        logger.warning("Відправка системного попередження підписникам про втрату зв'язку...")
        if self.bot:
            warning = (
                "⚠️ **УВАГА: Тимчасовий збій моніторингу**\n\n"
                "Втрачено з'єднання зі слухачем каналів. "
                "Автоматичні сповіщення тимчасово не надходять.\n"
                "Будь ласка, орієнтуйтеся на офіційну тривогу та повідомлення ОВА."
            )
            await broadcast_system_warning(self.bot, self.subscriber_repo, warning)

    async def on_connection_restored(self) -> None:
        logger.info("Відправка повідомлення про відновлення зв'язку...")
        if self.bot:
            msg = "🟢 **З'єднання з моніторингом відновлено.** Система працює в штатному режимі."
            await broadcast_system_warning(self.bot, self.subscriber_repo, msg)

    async def on_siren_start(self, state: SirenState) -> None:
        logger.warning(f"🚨 [ПОВІТРЯНА ТРИВОГА] Оголошено тривогу: {state.region}")
        if self.bot:
            time_str = now_kyiv().strftime("%H:%M")
            title = "🚨🔴 **ПОВІТРЯНА ТРИВОГА!**"
            body = (
                f"В **{state.region}** оголошено офіційну повітряну тривогу (о {time_str})!\n\n"
                f"Негайно прямуйте в укриття або перебувайте в безпечному місці!\n"
                f"Слідкуйте за офіційними повідомленнями ОВА та Повітряних сил."
            )
            await broadcast_siren_event(self.bot, self.subscriber_repo, title, body, is_silent=False)

    async def on_siren_end(self, state: SirenState) -> None:
        logger.info(f"🟢 [ВІДБІЙ ТРИВОГИ] Відбій повітряної тривоги: {state.region}")
        if self.bot:
            time_str = now_kyiv().strftime("%H:%M")
            title = "🟢 **ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ**"
            body = (
                f"В **{state.region}** оголошено відбій повітряної тривоги (о {time_str}).\n\n"
                f"Загроза минула. Будьте уважні та бережіть себе!"
            )
            await broadcast_siren_event(self.bot, self.subscriber_repo, title, body, is_silent=False)

    async def run(self) -> None:
        logger.info("Запуск сервісу попередження про реактивні БпЛА та повітряні тривоги...")

        tasks = []

        # Background FastAPI server (healthcheck)
        config = uvicorn.Config(
            app=api_app,
            host=settings.api_host,
            port=settings.api_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        tasks.append(asyncio.create_task(server.serve()))

        # Start the air-raid alert monitor in the background
        if self.siren_service:
            tasks.append(asyncio.create_task(self.siren_service.start()))

        # Start the Telethon listener in the background
        if self.listener:
            tasks.append(asyncio.create_task(self.listener.start()))

        # Start the bot in the background
        if self.bot and self.bot_dp:
            tasks.append(asyncio.create_task(self.bot_dp.start_polling(self.bot)))

        # Wait for completion
        await self.stop_event.wait()

        # Stop the components (graceful shutdown)
        logger.info("Виконується зупинка сервісу...")
        if self.siren_service:
            await self.siren_service.stop()
        if self.listener:
            await self.listener.stop()
        if self.bot:
            await self.bot.session.close()
        server.should_exit = True
        self.learning_store.close()
        logger.info("Сервіс успішно зупинено.")


async def main():
    app = Application()

    def handle_sig():
        app.stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_sig)
        except NotImplementedError:
            pass

    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
