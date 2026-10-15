"""Telegram bot for user alerts and onboarding of the beta group."""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import BotCommand, Message

from src.engine.models import AlertEvent, AlertLevel, DroneType
from src.learning.text import format_place_title
from src.profile import get_profile
from src.telegram.feedback import FeedbackRepository, register_feedback_handlers
from src.utils.time import format_kyiv_time, now_kyiv

logger = logging.getLogger(__name__)


class SubscriberRepository:
    """Simple storage of bot subscribers with privacy protection (the /stop command deletes the data)."""

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


def create_bot_dispatcher(
    subscribers: SubscriberRepository,
    tracker=None,
    siren_service=None,
    feedback_repo: FeedbackRepository | None = None,
    feedback_chat_id: int | None = None,
    feedback_admin_ids: list[int] | None = None,
) -> Dispatcher:
    dp = Dispatcher()

    if feedback_repo is None:
        from src.config import settings
        feedback_repo = FeedbackRepository(settings.subscribers_db_path)

    from src.config import settings
    f_chat_id = feedback_chat_id if feedback_chat_id is not None else settings.feedback_chat_id
    f_admin_ids = feedback_admin_ids if feedback_admin_ids is not None else settings.feedback_admin_ids
    register_feedback_handlers(dp, feedback_repo, feedback_chat_id=f_chat_id, admin_ids=f_admin_ids)

    def get_levels_text() -> str:
        texts = get_profile().texts
        ref = tracker._get_ref() if tracker else None
        if ref and ref.points:
            groups = {
                1: ("🟡 **Рівень 1 — Потенційна загроза на підходах (тихе сповіщення):**", []),
                2: ("🟠 **Рівень 2 — Наближення (гучне сповіщення):**", []),
                3: ("🔴 **Рівень 3 — Безпосередня небезпека (термінове гучне сповіщення):**", []),
            }
            regional_items = []
            for p in sorted(ref.points.values(), key=lambda pt: pt.distance_km or 0.0, reverse=True):
                if getattr(p, "is_regional", False):
                    p_name = format_place_title(p.names[0]) if p.names else p.id
                    dist_str = f"~{p.distance_km:.0f} км" if p.distance_km else "—"
                    sound_note = "гучне сповіщення" if (p.distance_km or 0.0) <= 30.0 else "тихе сповіщення"
                    regional_items.append(f"• **{p_name}:** {dist_str} від ІФ ({sound_note}) | загроза району області")
                elif p.level in groups:
                    p_name = format_place_title(p.names[0]) if p.names else p.id
                    eta_str = f"**{p.eta_min:.0f}–{p.eta_max:.0f} хв**" if p.eta_min is not None and p.eta_max is not None else "—"
                    extra = " | рубежі передмістя" if p.id in ("khryplyn", "tysmenytsia") else ""
                    groups[p.level][1].append(f"• **{p_name}:** ~{p.distance_km:.0f} км{extra} | орієнтовно {eta_str}")
            res_blocks = [
                title + "\n" + "\n".join(items)
                for title, items in groups.values()
                if items
            ]
            if regional_items:
                res_blocks.append(
                    "ℹ️ **Регіональні вектори загроз (ймовірна тривога в області):**\n"
                    + "\n".join(regional_items)
                )
            res = "\n\n".join(res_blocks)
            if res.strip():
                return res

        return "\n\n".join(
            level["title"] + "\n" + "\n".join(level["items"]) for level in texts["howitworks_levels"]
        )

    def get_eta_text() -> str:
        texts = get_profile().texts
        levels = get_levels_text()
        return (
            "⏱ **ТАБЛИЦЯ РУБЕЖІВ ТА ОРІЄНТОВНИЙ ЧАС ПІДЛЬОТУ (ETA)**\n\n"
            f"Розраховано для руху БпЛА в напрямку {texts['city_gen']} "
            f"вздовж коридору {texts['route_short']}.\n\n"
            "• 🚀 **Реактивні БпЛА (400–480 км/год):** орієнтовні інтервали наведено в таблиці рубежів нижче.\n"
            "• 🛩 **Звичайні Шахеди (150–185 км/год):** час підльоту довший приблизно у 2.5 рази і розраховується автоматично при виявленні цілі.\n\n"
            f"{levels}\n\n"
            "⚠️ **Головне правило:** якщо оголошено тривогу — негайно в укриття, незалежно від повідомлень бота. "
            "Відсутність сповіщення не означає, що небезпеки немає.\n\n"
            "Детальніше про систему: /howitworks\n"
            "Повний дисклеймер: /disclaimer"
        )

    def get_howitworks_text() -> str:
        texts = get_profile().texts
        levels = get_levels_text()
        return (
            "ℹ️ **ЯК ПРАЦЮЄ СИСТЕМА ТА ЯК МИ РАХУЄМО ЧАС (ETA)**\n\n"
            f"Бот створений для завчасного інформування мешканців {texts['city_gen']} та прилеглих районів "
            "про потенційну загрозу реактивних і звичайних ударних БпЛА, що рухаються транзитним коридором "
            f"({texts['route_regions']}).\n\n"
            "📡 **1. Джерела моніторингу (канали, які відстежуються):**\n"
            "Система в реальному часі аналізує повідомлення з офіційних та перевірених каналів:\n"
            "• [@monitoring_zaxid](https://t.me/monitoring_zaxid) — Моніторинг Захід (найбільш оперативне джерело по Заходу)\n"
            "• [@kpszsu](https://t.me/kpszsu) — Повітряні Сили ЗС України (офіційне джерело)\n"
            "• [@kudy_letyt](https://t.me/kudy_letyt) — Куди летить?\n"
            "• [@monitorwarr](https://t.me/monitorwarr) — Monitor War\n"
            "• Офіційні тривоги бот бере окремо від каналів — із сервісу повітряних тривог.\n\n"
            "🧠 **2. Як працює виявлення цілей:**\n"
            "• Щойно в одному з каналів зʼявляється інформація про рух у бік населеного пункту з нашого коридору — "
            "система **миттєво** надсилає сповіщення.\n"
            "• До кожного повідомлення додається пряме посилання на першоджерело в Telegram для швидкої перевірки.\n"
            "• Кількість цілей визначається строго в межах контексту знайденого пункту, "
            "щоб виключити помилки зі зведених постів для інших областей.\n\n"
            "⏱ **3. Як розраховується орієнтовне вікно підльоту (ETA):**\n"
            "• **Тип загрози:** Система розпізнає швидкісні реактивні дрони (Шахед-238, швидкість 400–480 км/год) та звичайні ударні БпЛА (Шахед-136, швидкість 150–185 км/год).\n"
            "• **Маневри:** Політ рідко є прямолінійним. Дрони змінюють висоту і курс для огинання зон ППО, "
            "тому до дистанції закладається коефіцієнт маневрування.\n"
            "• **Розрахункове вікно:** Вказується діапазон від найшвидшого прямого дольоту до руху з маневрами "
            "(наприклад, `22:50–22:58`, що відповідає `26–33 хв`).\n"
            "• **Часовий пояс:** Усі часові мітки та вікна підльоту подаються за **Київським часом** (`Europe/Kyiv`).\n\n"
            f"📊 **4. Актуальний довідник рубежів до {texts['city_gen']}:**\n\n"
            f"{levels}\n\n"
            "⚠️ **5. Головне правило:** якщо оголошено тривогу — негайно в укриття, незалежно від повідомлень бота. Повний дисклеймер: /disclaimer"
        )

    def get_disclaimer_text() -> str:
        return (
            "⚠️ **БЕЗПЕКОВЕ ЗАСТЕРЕЖЕННЯ (ДИСКЛЕЙМЕР)**\n\n"
            "**Головне правило.** Якщо оголошено повітряну тривогу — негайно прямуйте в безпечне місце або в укриття. "
            "Не залежить від того, чи є повідомлення від бота: відсутність сповіщення не означає, що небезпеки немає.\n\n"
            "**1. Статус бота**\n"
            "• Бот не є офіційним джерелом. Це автоматизований волонтерський інструмент раннього оповіщення, який дає додатковий час на реагування.\n"
            "• Бот ніколи не оголошує і не скасовує тривогу. Офіційні повідомлення Повітряних Сил ЗСУ, ДСНС та ОВА мають абсолютний пріоритет.\n\n"
            "**2. Точність часу підльоту**\n"
            "• Розрахунковий час — це математична екстраполяція за повідомленнями каналів, а не гарантія.\n"
            "• Реальний рух цілі може змінитися: курс, висота, віражі, знищення силами ППО.\n"
            "• Жоден алгоритм не гарантує точності до хвилини, тому не сприймайте цей час як істину.\n\n"
            "**3. Можливі помилки**\n"
            "• Бот перебуває в режимі донавчання, тому можливі хибні спрацювання, а також пропущені цілі.\n"
            "• Якщо бот повідомляє, що ціль прямує у ваш бік, а офіційної тривоги ще немає — не ігноруйте це. "
            "Перевірте першоджерело за посиланням під сповіщенням і керуйтеся здоровим глуздом. Безпека — у пріоритеті."
        )

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        subscribers.add(message.chat.id)
        # Check deep-link parameters: /start howitworks, /start eta, /start disclaimer
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1:
            arg = parts[1].strip().lower()
            if arg in ("howitworks", "help"):
                await message.answer(get_howitworks_text(), parse_mode="Markdown", disable_web_page_preview=True)
                return
            if arg in ("eta", "calc", "table"):
                await message.answer(get_eta_text(), parse_mode="Markdown", disable_web_page_preview=True)
                return
            if arg in ("disclaimer", "warning", "rules"):
                await message.answer(get_disclaimer_text(), parse_mode="Markdown")
                return

        texts = get_profile().texts
        text = (
            f"👋 **Вітаємо!** Це волонтерський бот раннього оповіщення про загрозу ударних БпЛА для {texts['city_gen']}.\n\n"
            "**Що він робить**\n"
            f"1. 🚨 Передає офіційні повітряні тривоги та відбої по {texts['region_loc']}.\n"
            f"2. ⚡️ Стежить за моніторинговими каналами й попереджає, коли реактивний дрон чи Шахед рухається коридором {texts['route_short']}, з орієнтовним вікном підльоту (ETA).\n\n"
            "**Головне правило**\n"
            "Бот — додатковий сигнал, а не офіційне джерело. Якщо оголошено тривогу — негайно в укриття, незалежно від того, чи писав бот. Відсутність сповіщення не означає, що небезпеки немає.\n\n"
            "**Як це виглядає**\n"
            "🟡 без звуку — ціль на підходах · 🟠🔴 зі звуком — наближення й небезпека. Щоб чути їх уночі, додайте бота до винятків «Не турбувати» (перевірка: /test).\n\n"
            "Бот працює в бета-режимі й донавчається, тому можливі хибні спрацювання та пропуски.\n\n"
            "**Команди**\n"
            "/status — стан тривоги та активні цілі\n"
            "/eta — таблиця рубежів і орієнтовний час\n"
            "/howitworks — як це працює\n"
            "/test — перевірити звук і сповіщення\n"
            "/feedback — написати розробнику\n"
            "/disclaimer — безпекове застереження\n"
            "/stop — вимкнути сповіщення й видалити мої дані"
        )
        await message.answer(text, parse_mode="Markdown")

    @dp.message(Command("howitworks"))
    async def cmd_howitworks(message: Message):
        await message.answer(get_howitworks_text(), parse_mode="Markdown", disable_web_page_preview=True)

    @dp.message(Command("eta"))
    async def cmd_eta(message: Message):
        await message.answer(get_eta_text(), parse_mode="Markdown", disable_web_page_preview=True)

    @dp.message(Command("disclaimer"))
    async def cmd_disclaimer(message: Message):
        await message.answer(get_disclaimer_text(), parse_mode="Markdown")

    @dp.message(Command("test"))
    async def cmd_test(message: Message):
        test_alert = (
            "🧪 **[ТЕСТ] Технічне сповіщення**\n\n"
            "Це перевірка доставки та звуку. ⚠️ **Реальної загрози немає.**\n\n"
            "**Налаштування**\n"
            "1. Увімкніть звук сповіщень для цього чату.\n"
            "2. Закріпіть чат угорі списку.\n"
            "3. Додайте бота до винятків «Не турбувати», якщо хочете чути сигнали вночі.\n\n"
            "🟡 Сповіщення рівня 1 («потенційна загроза») приходять без звуку — так задумано. "
            "Звук мають 🟠 «наближення» та 🔴 «небезпека»."
        )
        await message.answer(test_alert, parse_mode="Markdown")

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        lines = []

        # 1. Official air-raid alert
        siren = siren_service.get_current_state() if siren_service else None
        if siren:
            if siren.is_active:
                siren_line = f"🚨 **Офіційна повітряна тривога:** ТРИВОГА ({siren.region})"
                if siren.changed_at and siren.changed_at != "1970-01-01 03:00:00":
                    siren_line += f"\n  (оголошено: {siren.changed_at})"
            else:
                siren_line = f"🟢 **Офіційна повітряна тривога:** Відбій ({siren.region})"
                if siren.changed_at and siren.changed_at != "1970-01-01 03:00:00":
                    siren_line += f"\n  (відбій о: {siren.changed_at})"
        else:
            siren_line = "⚪️ **Офіційна повітряна тривога:** моніторинг активний"
        lines.append(siren_line)
        lines.append("")

        # 2. Early detection of jet drones
        active = tracker.get_active_targets() if tracker else []
        ref = tracker._get_ref() if tracker else None
        if not active:
            lines.append("🟢 **Активних цілей на коридорі не зафіксовано.**")
        else:
            lines.append(f"⚠️ **Зафіксовано активних цілей на коридорі: {len(active)}**\n")
            level_labels = {
                AlertLevel.LEVEL_1: "🟡 Потенційна загроза",
                AlertLevel.LEVEL_2: "🟠 Наближення",
                AlertLevel.LEVEL_3A: "🔴 Небезпека (підліт)",
                AlertLevel.LEVEL_3B: "🔴 Небезпека біля / над містом",
                AlertLevel.REGIONAL: "ℹ️ Загроза в області (ймовірна тривога)",
            }
            for idx, t in enumerate(active, 1):
                eta_str = f"{t.eta_min_minutes:.0f}–{t.eta_max_minutes:.0f} хв"
                if t.eta_window_start_utc and t.eta_window_end_utc:
                    s_k = format_kyiv_time(t.eta_window_start_utc)
                    e_k = format_kyiv_time(t.eta_window_end_utc)
                    eta_str = f"{s_k}–{e_k} ({eta_str})"
                last_seen_k = format_kyiv_time(t.last_seen_utc)

                point_name = format_place_title(t.last_point_id)
                if ref and t.last_point_id in ref.points and ref.points[t.last_point_id].names:
                    point_name = format_place_title(ref.points[t.last_point_id].names[0])
                else:
                    profile = get_profile()
                    for pt in profile.points:
                        if pt.get("id") == t.last_point_id and pt.get("names"):
                            point_name = format_place_title(pt["names"][0])
                            break

                lvl_display = level_labels.get(t.level, t.level.value)
                drone_label = "Шахед" if getattr(t, "drone_type", None) == DroneType.CONVENTIONAL else "реактивний"
                if t.level == AlertLevel.REGIONAL:
                    threat_line = "  Очікування: **загроза району області (курс не на Франківськ)**\n"
                else:
                    threat_line = f"  Очікування біля міста: **{eta_str}**\n"
                lines.append(
                    f"• **Ціль #{idx}** ({t.target_count}х • {drone_label})\n"
                    f"  Останній пункт: **{point_name}** (о {last_seen_k})\n"
                    f"  Статус: {lvl_display}\n"
                    f"{threat_line}"
                )

        now_str = now_kyiv().strftime("%H:%M:%S")
        ver_str = f" • Довідник: v{ref.version:04d}" if ref else ""
        lines.append(f"\n🕒 _Станом на {now_str} (Київ){ver_str}_")
        await message.answer("\n".join(lines), parse_mode="Markdown")

    @dp.message(Command("stop"))
    async def cmd_stop(message: Message):
        subscribers.remove(message.chat.id)
        if feedback_repo:
            feedback_repo.delete_user_tickets(message.chat.id)
        await message.answer(
            "Сповіщення вимкнено. Ваш Telegram ID видалено зі списку отримувачів і з бази звернень.\n"
            "Щоб відновити сповіщення, надішліть /start.",
            parse_mode="Markdown",
        )

    return dp


async def setup_bot_commands(bot: Bot) -> None:
    """Register bot commands in Telegram Menu."""
    commands = [
        BotCommand(command="status", description="Стан тривоги та активні цілі"),
        BotCommand(command="eta", description="Таблиця рубежів та орієнтовний час"),
        BotCommand(command="howitworks", description="Як працює система та моніторинг"),
        BotCommand(command="test", description="Перевірити сповіщення та звук"),
        BotCommand(command="feedback", description="Зворотний звʼязок та помилки"),
        BotCommand(command="disclaimer", description="Безпекове застереження"),
        BotCommand(command="stop", description="Зупинити отримання сповіщень"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не вдалося встановити команди бота: %s", exc)


async def broadcast_alert(bot: Bot, subscribers: SubscriberRepository, event: AlertEvent) -> None:
    """Send a compact alert to all subscribers with direct links to the sources."""
    chat_ids = subscribers.get_all()
    if not chat_ids:
        return

    # Get the bot name to build convenient deep-link URLs
    bot_username = "simpledronealert_bot"
    try:
        me = await bot.get_me()
        if me and me.username:
            bot_username = me.username
    except Exception as exc:  # noqa: BLE001 - fallback bot name, not critical
        logger.warning("Не вдалося отримати імʼя бота, використано типове: %s", exc)

    parts = [f"**{event.title}**"]
    if event.body:
        parts.append(event.body)

    # Add clickable links to the sources (original Telegram posts)
    if event.source_links:
        src_items = [f"[{ch}]({url})" for ch, url in event.source_links]
        parts.append(f"🔗 Джерело: {', '.join(src_items)}")

    # For combat levels, add navigation links to "Як рахуємо" and "Дисклеймер"
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
    """Send an alert about an official air-raid alert or its all-clear."""
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
    """Send a system warning about a monitoring failure or about connection recovery."""
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
