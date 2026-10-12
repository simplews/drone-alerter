"""Telegram-бот для сповіщення користувачів та онбордингу бета-групи."""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

from src.engine.models import AlertEvent, AlertLevel
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

    def get_eta_help_text() -> str:
        ref = tracker._get_ref() if tracker and hasattr(tracker, "_get_ref") else None
        lines = [
            "ℹ️ **Як ми рахуємо розрахунковий час (ETA)?**\n",
            "1. **Модель швидкості:** Розрахунок базується на середній крейсерській швидкості реактивних БпЛА (450–650 км/год) з урахуванням маневрів та огинання рельєфу.",
            "2. **Точка відліку:** Час розраховується від моменту фіксації цілі в конкретному населеному пункті до меж Івано-Франківська.",
            "3. **Діапазон вікна:** Вказується мінімальний та максимальний час підльоту (наприклад, `13:31–13:38`, що відповідає `25–32 хв`).\n",
            "📊 **Актуальний довідник рубежів:**"
        ]
        if ref and hasattr(ref, "points"):
            for pid, pt in sorted(ref.points.items(), key=lambda x: -(x[1].distance_km or 0)):
                name = pt.names[0].capitalize() if pt.names else pid
                dist = f"{pt.distance_km:.0f} км" if pt.distance_km else "—"
                eta_r = f"{pt.eta_min_minutes:.0f}–{pt.eta_max_minutes:.0f} хв" if pt.eta_min_minutes else "—"
                lvl = f"Рівень {pt.level}"
                lines.append(f"• **{name}**: {dist} | ETA: {eta_r} ({lvl})")
        else:
            lines.append("• Нова Ушиця: ~190 км | ETA: 25–35 хв")
            lines.append("• Борщів: ~85 км | ETA: 12–20 хв")
            lines.append("• Тлумач: ~25 км | ETA: 4–8 хв")

        lines.append("\n⚠️ Більше про обмеження розрахунків: /disclaimer")
        return "\n".join(lines)

    def get_disclaimer_text() -> str:
        return (
            "⚠️ **БЕЗПЕКОВЕ ЗАСТЕРЕЖЕННЯ (ДИСКЛЕЙМЕР)**\n\n"
            "• **Не є офіційним джерелом:** Цей бот є автоматизованим волонтерським інструментом раннього оповіщення, створеним для надання додаткового часу на реагування.\n"
            "• **Маневри та зміна траєкторії:** Розрахунковий час є суто математичною екстраполяцією. Реальний рух цілі може змінюватися через зміну курсу, висоти, закручування віражів або знищення силами ППО.\n"
            "• **Не орієнтуйтеся як на істину:** Жоден алгоритм не може гарантувати точний час до секунди. Якщо оголошено тривогу або ціль прямує у ваш бік — **негайно прямуйте в безпечне місце або в укриття!**\n"
            "• **Бот ніколи не скасовує тривогу:** Офіційні повідомлення Повітряних Сил ЗСУ та ОВА мають абсолютний пріоритет."
        )

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        subscribers.add(message.chat.id)
        # Перевірка deep-link параметрів: /start eta або /start disclaimer
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1:
            arg = parts[1].strip().lower()
            if arg in ("eta", "help_eta", "calc"):
                await message.answer(get_eta_help_text(), parse_mode="Markdown")
                return
            if arg in ("disclaimer", "warning", "rules"):
                await message.answer(get_disclaimer_text(), parse_mode="Markdown")
                return

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
            "• **Рівень 1 (Потенційна загроза на підходах):** беззвучні повідомлення (silent), щоб не турбувати марно.\n\n"
            "📱 **Порада для iOS / Android:**\n"
            "Додайте цього бота у список дозволених контактів у режимі **«Не турбувати» (Focus / DND)**, "
            "щоб не пропустити критичні сигнали.\n\n"
            "Команди:\n"
            "/status — поточний стан тривоги та активні цілі\n"
            "/eta — як ми рахуємо час та актуальна таблиця рубежів\n"
            "/disclaimer — безпековий дисклеймер\n"
            "/test — надіслати тестове сповіщення для перевірки звуку/DND\n"
            "/stop — зупинити сповіщення та повністю видалити мій акаунт"
        )
        await message.answer(text, parse_mode="Markdown")

    @dp.message(Command("eta"))
    async def cmd_eta(message: Message):
        await message.answer(get_eta_help_text(), parse_mode="Markdown")

    @dp.message(Command("disclaimer"))
    async def cmd_disclaimer(message: Message):
        await message.answer(get_disclaimer_text(), parse_mode="Markdown")

    @dp.message(Command("test"))
    async def cmd_test(message: Message):
        now_k = now_kyiv()
        t_start = (now_k + timedelta(minutes=8)).strftime("%H:%M")
        t_end = (now_k + timedelta(minutes=15)).strftime("%H:%M")
        test_alert = (
            "🧪 **ТЕСТОВЕ СПОВІЩЕННЯ СИСТЕМИ (Перевірка звуку)**\n\n"
            "🟠 **Наближення: Товсте**\n"
            f"⏱ Очікується біля міста: **{t_start}–{t_end}** (за 8–15 хв)\n"
            "🔗 Джерело: [test_channel #1](https://t.me/monitoring_zaxid)\n\n"
            "ℹ️ [Як ми рахуємо](https://t.me/simpledronealert_bot?start=eta) • [Застереження](https://t.me/simpledronealert_bot?start=disclaimer)"
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
    """Розсилає компактне сповіщення всім підписникам із прямими посиланнями на джерела."""
    chat_ids = subscribers.get_all()
    if not chat_ids:
        return

    # Отримуємо ім'я бота для формування зручних deep-link посилань
    bot_username = "simpledronealert_bot"
    try:
        me = await bot.get_me()
        if me and me.username:
            bot_username = me.username
    except Exception:
        pass

    parts = [f"**{event.title}**"]
    if event.body:
        parts.append(event.body)

    # Додаємо клікабельні посилання на джерела (першоджерела у Telegram)
    if event.source_links:
        src_items = [f"[{ch}]({url})" for ch, url in event.source_links]
        parts.append(f"🔗 Джерело: {', '.join(src_items)}")

    # Для бойових рівнів додаємо навігаційні посилання на "Як рахуємо" та "Дисклеймер"
    if event.level != AlertLevel.INFO:
        parts.append(
            f"\nℹ️ [Як ми рахуємо](https://t.me/{bot_username}?start=eta) • "
            f"[Застереження](https://t.me/{bot_username}?start=disclaimer)"
        )

    text = "\n".join(parts)

    for chat_id in chat_ids:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                disable_notification=event.is_silent,
                disable_web_page_preview=True,
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
