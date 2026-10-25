"""Utility to broadcast announcements to all subscribers via CLI or programmatic call."""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from src.config import settings
from src.telegram.bot import SubscriberRepository

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("broadcast_update")

DEFAULT_UPDATE_TEXT = """🔔 <b>Оновлення системи: нові рубежі підльоту та регіональний моніторинг (v9)</b>

Ми покращили алгоритми відстеження реактивних цілей на основі аналізу останніх прольотів:

🟠 <b>Нові рубежі 2-го рівня (наближення, 9–15 хв):</b>
• <b>Чортків</b> (~82 км) та <b>Заліщики</b> (~81 км) — додано як постійні опорні точки підходу з боку Тернопільщини та Дністра. Завдяки цьому рух через південний вектор тепер відстежується безперервно перед підходом до Бучача, Бурштина та Франківська.

🟡 <b>Розширено моніторинг загроз по області (Рівень 1):</b>
• До переліку районів області додано <b>Долину</b>, <b>Надвірну</b>, <b>Снятин</b> та <b>Войнилів</b>. Якщо ціль прямує в межах області в бік цих райцентрів, ви отримаєте своєчасне інформаційне попередження.

🧭 <b>Точніше визначення відхилень курсу:</b>
• Покращено виявлення маневрів у напрямку Львівщини (зокрема Стрийський вектор) та Буковини — це мінімізує зайві сповіщення, якщо борт відвертає від нашого напрямку.

ℹ️ <i>Повний список рубежів та інтервалів підльоту можна переглянути за командою /howitworks.</i>

🛡 <i>Нагадуємо: бот є системою раннього інформування і не скасовує необхідності перебувати в укриттях під час офіційної повітряної тривоги.</i>"""


async def broadcast_to_subscribers(
    text: str,
    bot: Bot,
    subscribers: SubscriberRepository,
    photo_file_id: str | None = None,
    parse_mode: str | None = "HTML",
    throttle_delay: float = 0.05,
) -> tuple[int, int]:
    """Broadcast a message (or photo with caption) to all bot subscribers.

    Returns:
        (success_count, failed_count)
    """
    chat_ids = subscribers.get_all()
    total = len(chat_ids)
    logger.info("Розпочато розсилку для %d підписників...", total)

    success = 0
    failed = 0

    for i, chat_id in enumerate(chat_ids, 1):
        try:
            if photo_file_id:
                try:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_file_id,
                        caption=text if text else None,
                        parse_mode=parse_mode,
                        disable_notification=False,
                    )
                except TelegramBadRequest:
                    # Fallback to plain text if markup parsing failed
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_file_id,
                        caption=text if text else None,
                        parse_mode=None,
                        disable_notification=False,
                    )
            else:
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=parse_mode,
                        disable_notification=False,
                    )
                except TelegramBadRequest:
                    # Fallback to plain text if markup parsing failed
                    await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=None,
                        disable_notification=False,
                    )
            success += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("[%d/%d] Не вдалося надіслати до chat_id %s: %s", i, total, chat_id, exc)

        if throttle_delay > 0:
            await asyncio.sleep(throttle_delay)

    logger.info("Розсилку завершено! Успішно: %d, помилок: %d", success, failed)
    return success, failed


async def run_cli(
    text: str,
    dry_run: bool = False,
    parse_mode: str | None = "HTML",
) -> None:
    """Execute the broadcast from CLI."""
    if not settings.telegram_bot_token and not dry_run:
        logger.error("TELEGRAM_BOT_TOKEN не задано в налаштуваннях")
        return

    repo = SubscriberRepository(settings.subscribers_db_path)
    count = repo.count()

    if dry_run:
        print(f"🔍 [DRY RUN] Кількість підписників: {count}")
        print(f"Режим розмітки: {parse_mode}")
        print("--- Текст повідомлення ---")
        print(text)
        print("--------------------------")
        return

    if count == 0:
        logger.warning("Немає підписників у базі %s", settings.subscribers_db_path)
        return

    bot = Bot(token=settings.telegram_bot_token)
    try:
        await broadcast_to_subscribers(
            text=text,
            bot=bot,
            subscribers=repo,
            parse_mode=parse_mode,
        )
    finally:
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Розсилка сповіщень усім підписникам бота")
    parser.add_argument(
        "text",
        nargs="?",
        default=None,
        help="Текст сповіщення (якщо не вказано, використовується стандартний текст оновлення або --file)",
    )
    parser.add_argument("-t", "--text", dest="text_flag", help="Текст сповіщення")
    parser.add_argument("-f", "--file", type=Path, help="Шлях до файлу з текстом сповіщення")
    parser.add_argument(
        "-p",
        "--parse-mode",
        default="HTML",
        choices=["HTML", "Markdown", "plain"],
        help="Режим форматування (за замовчуванням HTML)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Попередній перегляд без відправки повідомлень",
    )

    args = parser.parse_args()

    message_text: str
    if args.file:
        message_text = args.file.read_text(encoding="utf-8")
    elif args.text_flag:
        message_text = args.text_flag
    elif args.text:
        message_text = args.text
    else:
        message_text = DEFAULT_UPDATE_TEXT

    pm = None if args.parse_mode == "plain" else args.parse_mode
    asyncio.run(run_cli(message_text, dry_run=args.dry_run, parse_mode=pm))


if __name__ == "__main__":
    main()
