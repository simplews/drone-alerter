"""Telegram-бот для сповіщення користувачів та онбордингу бета-групи."""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
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

    def get_howitworks_text() -> str:
        return (
            "ℹ️ **ЯК ПРАЦЮЄ СИСТЕМА ТА ЯК МИ РАХУЄМО ЧАС (ETA)**\n\n"
            "Бот створений для завчасного інформування мешканців Івано-Франківська та прилеглих районів "
            "про потенційну загрозу реактивних БпЛА, що рухаються транзитним коридором "
            "(Вінниччина → Хмельниччина → Тернопільщина → Франківщина).\n\n"
            "📡 **1. Джерела моніторингу (канали, які відстежуються):**\n"
            "Система в реальному часі аналізує повідомлення з офіційних та перевірених каналів:\n"
            "• [@monitoring_zaxid](https://t.me/monitoring_zaxid) — Моніторинг Захід (найбільш оперативне джерело по Заходу)\n"
            "• [@kpszsu](https://t.me/kpszsu) — Повітряні Сили ЗС України (офіційне джерело)\n"
            "• [@kudy_letyt](https://t.me/kudy_letyt) — Куди летить?\n"
            "• [@monitorwarr](https://t.me/monitorwarr) — Monitor War\n\n"

            "🧠 **2. Як працює виявлення цілей:**\n"
            "• Щойно в одному з каналів зʼявляється інформація про рух у бік населеного пункту з нашого коридору — "
            "система **миттєво** надсилає сповіщення.\n"
            "• До кожного повідомлення додається пряме посилання на першоджерело в Telegram для швидкої перевірки.\n"
            "• Кількість цілей визначається строго в межах контексту знайденого пункту, "
            "щоб виключити помилки зі зведених постів для інших областей.\n\n"
            "⏱ **3. Як розраховується орієнтовне вікно підльоту (ETA):**\n"
            "• **Швидкість:** Середня крейсерська швидкість реактивних дронів складає **450–650 км/год** (близько 7.5–10.8 км/хв).\n"
            "• **Маневри:** Політ рідко є прямолінійним. Дрони змінюють висоту і курс для огинання зон ППО, "
            "тому до дистанції закладається коефіцієнт маневрування.\n"
            "• **Розрахункове вікно:** Вказується діапазон від найшвидшого прямого дольоту до руху з маневрами "
            "(наприклад, `13:31–13:38`, що відповідає `25–32 хв`).\n"
            "• **Часовий пояс:** Усі часові мітки та вікна підльоту подаються за **Київським часом** (`Europe/Kyiv`).\n\n"
            "📊 **4. Актуальний довідник рубежів до Івано-Франківська:**\n\n"
            "🟡 **Рівень 1 — Потенційна загроза на підходах:**\n"
            "• **Піщанка:** ~265 км | орієнтовно **34–45 хв**\n"
            "• **Вендичани:** ~225 км | орієнтовно **29–39 хв**\n"
            "• **Нова Ушиця:** ~190 км | орієнтовно **26–33 хв**\n"
            "• **Стара Ушиця:** ~165 км | орієнтовно **21–28 хв**\n"
            "• **Дунаївці:** ~150 км | орієнтовно **19–26 хв**\n\n"
            "🟠 **Рівень 2 — Наближення:**\n"
            "• **Кам’янець-Подільський:** ~135 км | орієнтовно **17–23 хв**\n"
            "• **Скала-Подільська:** ~110 км | орієнтовно **14–19 хв**\n"
            "• **Борщів:** ~85 км | орієнтовно **12–16 хв**\n"
            "• **Товсте:** ~65 км | орієнтовно **9–13 хв**\n"
            "• **Городенка:** ~45 км | орієнтовно **6–9 хв**\n\n"
            "🔴 **Рівень 3 — Безпосередня небезпека:**\n"
            "• **Чернелиця:** ~35 км | орієнтовно **4–7 хв**\n"
            "• **Тлумач:** ~25 км | орієнтовно **3–5 хв**\n"
            "• **Отинія / Коломия:** ~20–30 км | орієнтовно **3–6 хв**\n"
            "• **Тисмениця / Хриплин:** рубежі передмістя | орієнтовно **1–3 хв**\n\n"
            "⚠️ **5. Важливе застереження:**\n"
            "Час є суто математичним орієнтиром. За сигналом тривоги завжди прямуйте в безпечні місця!\n"
            "Повний дисклеймер: /disclaimer"
        )

    def get_disclaimer_text() -> str:
        return (
            "⚠️ **БЕЗПЕКОВЕ ЗАСТЕРЕЖЕННЯ (ДИСКЛЕЙМЕР)**\n\n"
            "• **Не є офіційним джерелом:** Цей бот є автоматизованим волонтерським інструментом раннього оповіщення, створеним для надання додаткового часу на реагування.\n\n"
            "• **Маневри та зміна траєкторії:** Розрахунковий час є суто математичною екстраполяцією. Реальний рух цілі може змінюватися через зміну курсу, висоти, закручування віражів або знищення силами ППО.\n\n"
            "• **Не орієнтуйтеся як на істину:** Жоден алгоритм не може гарантувати точний час до секунди. Якщо оголошено тривогу або ціль прямує у ваш бік — **негайно прямуйте в безпечне місце або в укриття!**\n\n"
            "• **Бот ніколи не скасовує тривогу:** Офіційні повідомлення Повітряних Сил ЗСУ та ОВА мають абсолютний пріоритет."
        )

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        subscribers.add(message.chat.id)
        # Перевірка deep-link параметрів: /start howitworks або /start disclaimer
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1:
            arg = parts[1].strip().lower()
            if arg in ("howitworks", "eta", "help_eta", "calc"):
                await message.answer(get_howitworks_text(), parse_mode="Markdown", disable_web_page_preview=True)
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
            "Команди:\n"
            "/status — поточний стан тривоги та активні цілі\n"
            "/howitworks — як працює система, канали моніторингу та таблиця рубежів\n"
            "/disclaimer — безпековий дисклеймер\n"
            "/test — надіслати тестове сповіщення\n"
            "/stop — зупинити сповіщення та видалити мій акаунт"
        )
        await message.answer(text, parse_mode="Markdown")

    @dp.message(Command("howitworks"))
    async def cmd_howitworks(message: Message):
        await message.answer(get_howitworks_text(), parse_mode="Markdown", disable_web_page_preview=True)

    @dp.message(Command("eta"))
    async def cmd_eta(message: Message):
        await message.answer(get_howitworks_text(), parse_mode="Markdown", disable_web_page_preview=True)

    @dp.message(Command("disclaimer"))
    async def cmd_disclaimer(message: Message):
        await message.answer(get_disclaimer_text(), parse_mode="Markdown")

    @dp.message(Command("test"))
    async def cmd_test(message: Message):
        test_alert = (
            "🧪 **[ТЕСТ] ТЕХНІЧНЕ СПОВІЩЕННЯ СИСТЕМИ**\n\n"
            "Це тестова перевірка доставки звуку та налаштувань сповіщень.\n"
            "⚠️ **Реальної загрози немає!**\n\n"
            "Дякуємо, що ви з нами! Бажаємо тихого і безпечного дня! 🕊"
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
    except Exception as exc:  # noqa: BLE001 - запасне імʼя бота, не критично
        logger.warning("Не вдалося отримати імʼя бота, використано типове: %s", exc)

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
            f"\nℹ️ [Як ми рахуємо](https://t.me/{bot_username}?start=howitworks) • "
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
