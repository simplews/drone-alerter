"""Utility to export the message history from the monitoring channels through Telethon.

Usage:
    python -m src.telegram.export_history --days 4 --output data/historical_posts.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

from src.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("export_history")


async def export_channel_history(
    api_id: int,
    api_hash: str,
    session_str: str,
    channels: list[str],
    since_utc: datetime,
    output_path: Path,
) -> None:
    all_posts = []

    logger.info(f"Підключення до Telegram через Telethon... (збір з {since_utc.isoformat()})")
    async with TelegramClient(StringSession(session_str), api_id, api_hash) as client:
        for channel in channels:
            logger.info(f"Вивантаження повідомлень з каналу @{channel}...")
            count = 0
            try:
                entity = await client.get_entity(channel)
                async for message in client.iter_messages(entity, offset_date=None, reverse=False):
                    if not message.date:
                        continue
                    msg_date_utc = message.date.astimezone(UTC)
                    if msg_date_utc < since_utc:
                        break  # reached the older messages

                    if message.text and message.text.strip():
                        all_posts.append({
                            "channel": channel,
                            "message_id": message.id,
                            "posted_at_utc": msg_date_utc.replace(microsecond=0).isoformat(),
                            "text": message.text.strip(),
                        })
                        count += 1
                logger.info(f"  Отримано {count} текстових повідомлень з @{channel}.")
            except Exception as e:  # noqa: BLE001
                logger.error(f"Помилка вивантаження з @{channel}: {e}")


    # Sort in chronological order
    all_posts.sort(key=lambda p: p["posted_at_utc"])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(all_posts, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Успішно збережено {len(all_posts)} повідомлень у {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Вивантаження історії каналів за останні дні")
    parser.add_argument("--days", type=int, default=5, help="Кількість днів назад (за замовчуванням 5)")
    parser.add_argument("--since", type=str, help="Початкова дата у форматі YYYY-MM-DD")
    parser.add_argument("--channels", nargs="+", help="Список каналів (без @)")
    parser.add_argument("--output", type=Path, default=Path("data/historical_posts.json"), help="Шлях для збереження")

    args = parser.parse_args()

    if not settings.telethon_api_id or not settings.telethon_api_hash:
        print("Помилка: TELETHON_API_ID та TELETHON_API_HASH мають бути задані в .env", flush=True)
        return

    if not settings.telethon_string_session:
        print("Помилка: TELETHON_STRING_SESSION має бути задана в .env (або згенерована через generate_session)", flush=True)
        return

    channels = args.channels or settings.monitored_channels

    if args.since:
        since_utc = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
    else:
        since_utc = datetime.now(UTC) - timedelta(days=args.days)

    asyncio.run(
        export_channel_history(
            api_id=settings.telethon_api_id,
            api_hash=settings.telethon_api_hash,
            session_str=settings.telethon_string_session,
            channels=channels,
            since_utc=since_utc,
            output_path=args.output,
        )
    )


if __name__ == "__main__":
    main()
