"""Скрипт для одноразової генерації StringSession для Telethon.

Запуск:
    python -m src.telegram.generate_session

Після проходження авторизації скрипт виведе рядок сесії, який необхідно
вставити у змінну TELETHON_STRING_SESSION у файлі .env (або в налаштуваннях хостингу).
"""
import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    print("=" * 60)
    print("Генератор StringSession для Telethon (Drone Alert IF)")
    print("=" * 60)

    api_id = os.environ.get("TELETHON_API_ID")
    if not api_id:
        api_id = input("Введіть ваш Telegram API ID (з my.telegram.org): ").strip()
    api_id = int(api_id)

    api_hash = os.environ.get("TELETHON_API_HASH")
    if not api_hash:
        api_hash = input("Введіть ваш Telegram API HASH: ").strip()

    print("\nПідключення до Telegram... Буде надіслано код підтвердження.")
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_str = client.session.save()
        print("\n" + "=" * 60)
        print("АВТОРИЗАЦІЯ УСПІШНА! Ось ваш рядок сесії:")
        print("=" * 60)
        print(session_str)
        print("=" * 60)
        print("Скопіюйте цей рядок та збережіть у файлі .env:")
        print(f"TELETHON_STRING_SESSION={session_str}\n")


if __name__ == "__main__":
    asyncio.run(main())
