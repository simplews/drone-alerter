"""Telegram-бот для сповіщення користувачів та онбордингу бета-групи."""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

from src.engine.models import AlertEvent
from src.utils.time import format_kyiv_time, now_kyiv

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


def create_bot_dispatcher(subscribers: SubscriberRepository, tracker=None, siren_service=None) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        subscribers.add(message.chat.id)
        text = (
            "👋 **Вітаємо в системі завчасного попередження про загрози на Івано-Франківськ!**\n\n"
            "🔔 **Що відстежує цей бот:**\n"
            "1. 🚨 **Офіційні повітряні тривоги** по Івано-Франківській області (оголошення та відбій).\n"
            "2. ⚡️ **Завчасне раннє попередження** про рух реактивних БпЛА вздовж коридору Поділля → Тернопільщина → Франківськ з розрахунком вікна підльоту (ETA).\n\n"
            "⚠️ **ВАЖЛИВО: Безпековий дисклеймер**\n"
            "• Додаток є аналітичним інструментом і ніколи не дає команди «безпечно».\n"
            "• Завжди переходьте в укриття за офіційним сигналом тривоги.\n\n"
            "🔔 **Політика сповіщень та звук:**\n"
            "• **Повітряна тривога, Рівні 2 та 3 (Наближення / Підліт):** гучні сповіщення з вібрацією.\n"
            "• **Рівень 1 (Вхід у коридор, Вінниччина):** беззвучні повідомлення (silent), щоб не турбувати марно.\n\n"
            "📱 **Порада для iOS / Android:**\n"
            "Додайте цього бота у список дозволених контактів у режимі **«Не турбувати» (Focus / DND)**, "
            "щоб не пропустити критичні сигнали.\n\n"
            "Команди:\n"
            "/status — поточний стан тривоги та активні цілі\n"
            "/test — надіслати тестове сповіщення для перевірки звуку/DND\n"
            "/stop — зупинити сповіщення та повністю видалити мій акаунт"
        )
        await message.answer(text, parse_mode="Markdown")

    @dp.message(Command("test"))
    async def cmd_test(message: Message):
        now_k = now_kyiv()
        t_start = (now_k + timedelta(minutes=8)).strftime("%H:%M")
        t_end = (now_k + timedelta(minutes=15)).strftime("%H:%M")
        test_alert = (
            "🧪 **ТЕСТОВЕ СПОВІЩЕННЯ СИСТЕМИ (Перевірка звуку)**\n\n"
            "🔴 **[Рівень 2] [2х] Ціль наближається (Товсте)**\n"
            f"⏱ **Розрахункове вікно біля міста:** {t_start}–{t_end} (за 8–15 хв)\n"
            "📍 **Напрямок:** Товсте → Городенка → Тлумач → Івано-Франківськ\n\n"
            "⚠️ *Час підльоту є виключно довідковим розрахунком і може змінюватися "
            "через маневри або зміну курсу. Не орієнтуйтеся на нього як на істину — переходьте в безпечні місця!*\n\n"
            "🔔 Це сповіщення надіслано з увімкненим звуком (не silent). "
            "Перевірте, чи спрацював сигнал на вашому телефоні у поточному режимі (DND/Focus)."
        )
        await message.answer(test_alert, parse_mode="Markdown")

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        lines = []

        # 1. Офіційна повітряна тривога
        siren = siren_service.get_current_state() if siren_service else None
        if siren:
            if siren.is_active:
                siren_line = f"🚨 **Офіційна повітряна тривога:** ТРИВОГА ({siren.region})"
                if siren.changed_at and siren.changed_at != "1970-01-01 03:00:00":
                    siren_line += f"\n  (зміна: {siren.changed_at})"
            else:
                siren_line = f"🟢 **Офіційна повітряна тривога:** Відбій ({siren.region})"
        else:
            siren_line = "⚪️ **Офіційна повітряна тривога:** моніторинг активний"
        lines.append(siren_line)
        lines.append("")

        # 2. Раннє виявлення реактивних БпЛА
        active = tracker.get_active_targets() if tracker else []
        if not active:
            lines.append("🟢 **Активних реактивних цілей на коридорі не зафіксовано.**")
        else:
            lines.append(f"⚠️ **Зафіксовано активних цілей на коридорі: {len(active)}**\n")
            for t in active:
                eta_str = f"{t.eta_min_minutes:.0f}–{t.eta_max_minutes:.0f} хв"
                if t.eta_window_start_utc and t.eta_window_end_utc:
                    s_k = format_kyiv_time(t.eta_window_start_utc)
                    e_k = format_kyiv_time(t.eta_window_end_utc)
                    eta_str = f"{s_k}–{e_k} ({eta_str})"
                last_seen_k = format_kyiv_time(t.last_seen_utc)
                lines.append(
                    f"• **Ціль {t.id}** ({t.target_count}х)\n"
                    f"  Останній пункт: {t.last_point_id} (о {last_seen_k})\n"
                    f"  Рівень: {t.level.value}\n"
                    f"  Вікно біля міста: {eta_str}\n"
                )

        now_str = now_kyiv().strftime("%H:%M:%S")
        lines.append(f"\n🕒 _Станом на {now_str} (Київ)_")
        await message.answer("\n".join(lines), parse_mode="Markdown")

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


async def broadcast_siren_event(
    bot: Bot,
    subscribers: SubscriberRepository,
    title: str,
    body: str,
    is_silent: bool = False,
) -> None:
    """Розсилає сповіщення про офіційну повітряну тривогу або її відбій."""
    chat_ids = subscribers.get_all()
    if not chat_ids:
        return

    text = f"{title}\n\n{body}"

    for chat_id in chat_ids:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                disable_notification=is_silent,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Не вдалося надіслати сповіщення тривоги {chat_id}: {exc}")


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
