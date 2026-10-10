"""Telegram-бот для сповіщення користувачів та онбордингу бета-групи."""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

from src.engine.models import AlertEvent

logger = logging.getLogger(__name__)


class SubscriberRepository:
    """Просте сховище підписників бота з дотриманням приватності (команда /stop видаляє дані)."""

    def __init__(self, db_path: str = "data/subscribers.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    joined_at_utc TEXT NOT NULL,
                    receive_level_1 INTEGER DEFAULT 1
                )
            """)

    def add(self, chat_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO subscribers(chat_id, joined_at_utc) VALUES (?, ?)",
                (chat_id, now),
            )

    def remove(self, chat_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))

    def get_all(self) -> list[int]:
        rows = self.conn.execute("SELECT chat_id FROM subscribers").fetchall()
        return [r["chat_id"] for r in rows]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]


def create_bot_dispatcher(subscribers: SubscriberRepository, tracker=None) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        subscribers.add(message.chat.id)
        text = (
            "👋 **Вітаємо в системі завчасного попередження про реактивні БпЛА на Івано-Франківськ!**\n\n"
            "⚠️ **ВАЖЛИВО: Безпековий дисклеймер**\n"
            "• Цей бот **НЕ замінює офіційну тривогу** та Повітряні сили ЗСУ.\n"
            "• Додаток є аналітичним інструментом і ніколи не дає команди «безпечно».\n"
            "• Завжди переходьте в укриття за офіційним сигналом тривоги.\n\n"
            "🔔 **Політика сповіщень та налаштування звуку:**\n"
            "• **Рівень 1 (Увага, вхід у коридор):** беззвучні повідомлення (silent), щоб не турбувати вночі.\n"
            "• **Рівні 2 та 3 (Наближення / Підліт):** гучні сповіщення з вібрацією.\n\n"
            "📱 **Порада для iOS / Android:**\n"
            "Щоб не пропустити критичні нічні сигнали, додайте цього бота у список дозволених контактів "
            "у режимі **«Не турбувати» (Focus / DND)**.\n\n"
            "Команди:\n"
            "/status — поточний стан і активні цілі\n"
            "/stop — зупинити сповіщення та повністю видалити мій акаунт"
        )
        await message.answer(text, parse_mode="Markdown")

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        active = tracker.get_active_targets() if tracker else []
        if not active:
            msg = "🟢 **Активних реактивних цілей на коридорі не зафіксовано.**"
        else:
            msg = f"⚠️ **Зафіксовано активних цілей: {len(active)}**\n\n"
            for t in active:
                msg += (
                    f"• **Ціль {t.id}** ({t.target_count}х)\n"
                    f"  Останній пункт: {t.last_point_id}\n"
                    f"  Рівень: {t.level.value}\n"
                    f"  Вікно біля міста: {t.eta_min_minutes:.0f}–{t.eta_max_minutes:.0f} хв\n\n"
                )
        await message.answer(msg, parse_mode="Markdown")

    @dp.message(Command("stop"))
    async def cmd_stop(message: Message):
        subscribers.remove(message.chat.id)
        await message.answer(
            "Всі сповіщення зупинено. Ваш Telegram ID повністю видалено з бази.",
            parse_mode="Markdown",
        )

    return dp


async def broadcast_alert(bot: Bot, subscribers: SubscriberRepository, event: AlertEvent) -> None:
    """Розсилає сповіщення всім підписникам із дотриманням правил гучності."""
    chat_ids = subscribers.get_all()
    if not chat_ids:
        return

    text = f"**{event.title}**\n\n{event.body}"

    for chat_id in chat_ids:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                disable_notification=event.is_silent,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Не вдалося надіслати сповіщення {chat_id}: {exc}")


async def broadcast_system_warning(bot: Bot, subscribers: SubscriberRepository, text: str) -> None:
    """Розсилає системне попередження про збій моніторингу або відновлення зв'язку."""
    chat_ids = subscribers.get_all()
    for chat_id in chat_ids:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                disable_notification=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Помилка надсилання системного попередження {chat_id}: {exc}")
