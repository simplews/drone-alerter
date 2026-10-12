"""CLI-утиліта для запуску тестових сповіщень у Telegram-бот (drone / siren / all_clear)."""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta

import httpx

from src.config import settings
from src.engine.models import AlertEvent, AlertLevel
from src.telegram.bot import (
    SubscriberRepository,
    broadcast_alert,
    broadcast_siren_event,
)
from src.utils.time import now_kyiv


async def send_via_api(api_url: str, alert_type: str) -> bool:
    """Пробує надіслати тестове сповіщення через активний HTTP API сервера."""
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.post(f"{api_url}/test-alert?type={alert_type}")
            if resp.status_code == 200:
                data = resp.json()
                print(f"✅ Успішно надіслано через API: {data}")
                return True
            else:
                print(f"⚠️ API повернув код {resp.status_code}: {resp.text}")
    except Exception as exc:  # noqa: BLE001
        print(f"ℹ️ Не вдалося з'єднатися з API ({exc}), надсилаємо напряму через Telegram...")
    return False


async def send_directly(alert_type: str) -> None:
    """Надсилає тестове сповіщення напряму підписникам через токен бота."""
    from aiogram import Bot

    if not settings.telegram_bot_token:
        print("❌ Помилка: TELEGRAM_BOT_TOKEN не задано в .env")
        sys.exit(1)

    repo = SubscriberRepository(settings.subscribers_db_path)
    count = repo.count()
    if count == 0:
        print(f"⚠️ Немає підписників у базі {settings.subscribers_db_path}")
        return

    print(f"📢 Надсилаємо тестове сповіщення ({alert_type}) для {count} підписників...")
    bot = Bot(token=settings.telegram_bot_token)

    try:
        now_k = now_kyiv()
        time_str = now_k.strftime("%H:%M")

        if alert_type == "siren":
            title = "🧪🚨🔴 **[ТЕСТ] ПОВІТРЯНА ТРИВОГА!**"
            body = (
                f"Це тестова перевірка системи сповіщень про повітряну тривогу в **Івано-Франківській області** (о {time_str}).\n\n"
                "⚠️ Реальної небезпеки немає — це перевірка звуку та доставки.\n"
                "Перевірте, чи спрацював звуковий сигнал/вібрація."
            )
            await broadcast_siren_event(bot, repo, title, body, is_silent=False)
        elif alert_type == "all_clear":
            title = "🧪🟢 **[ТЕСТ] ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ**"
            body = (
                f"Це тестова перевірка сповіщення про відбій повітряної тривоги в **Івано-Франківській області** (о {time_str}).\n\n"
                "Тест доставки сигналу відбою."
            )
            await broadcast_siren_event(bot, repo, title, body, is_silent=False)
        else:  # "drone"
            t_start = (now_k + timedelta(minutes=8)).strftime("%H:%M")
            t_end = (now_k + timedelta(minutes=15)).strftime("%H:%M")
            window_str = f"{t_start}–{t_end} (за 8–15 хв)"
            event = AlertEvent(
                target_id="TEST-TARGET",
                level=AlertLevel.LEVEL_2,
                title="🧪🔴 [ТЕСТ] [2х] Ціль наближається (Товсте)",
                body=(
                    "Ціль прямує в бік Франківщини (Товсте)!\n"
                    f"Очікується біля міста: {window_str}.\n"
                    "Підготуйтеся перейти в безпечне місце.\n\n"
                    "⚠️ *Час підльоту є виключно довідковим розрахунком і може змінюватися "
                    "через маневри або зміну курсу. Не орієнтуйтеся на нього як на істину — переходьте в безпечні місця!*\n\n"
                    "🔔 *Це тестове сповіщення системи з увімкненим звуком.*"
                ),
                is_silent=False,
                eta_window_str=window_str,
            )
            await broadcast_alert(bot, repo, event)

        print("✅ Тестове сповіщення успішно надіслано!")
    finally:
        await bot.session.close()


async def main():
    parser = argparse.ArgumentParser(description="Надіслати тестове сповіщення у бот")
    parser.add_argument(
        "--type",
        choices=["drone", "siren", "all_clear"],
        default="drone",
        help="Тип сповіщення (drone | siren | all_clear)",
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8080",
        help="Адреса HTTP API додатка",
    )
    args = parser.parse_args()

    # Спочатку пробуємо через запущений API, якщо ні — напряму
    ok = await send_via_api(args.api_url, args.type)
    if not ok:
        await send_directly(args.type)


if __name__ == "__main__":
    asyncio.run(main())
