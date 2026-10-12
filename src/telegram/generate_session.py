"""Script to generate a StringSession for Telethon (one-time use).

It supports authorization through a QR code (no SMS and no code wait)
or through a phone number.

Run:
    python -m src.telegram.generate_session
"""
from __future__ import annotations

import asyncio
import os

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession


async def main():
    print("=" * 65)
    print("   ГЕНЕРАТОР STRINGSESSION ДЛЯ TELETHON (DRONE ALERT IF)")
    print("=" * 65)

    api_id_val = os.environ.get("TELETHON_API_ID")
    while not api_id_val:
        raw = input("Введіть ваш Telegram API ID (лише цифри з my.telegram.org): ").strip()
        if ":" in raw:
            print(
                "\n⚠️ Увага: ви ввели токен бота (з @BotFather).\n"
                "Токен бота зберігається у змінній TELEGRAM_BOT_TOKEN у .env.\n"
                "А для підключення до каналів (Telethon) потрібен числовий API ID з https://my.telegram.org\n"
            )
            continue
        try:
            api_id_val = int(raw)
        except ValueError:
            print("❌ API ID має бути цілим числом (наприклад: 12345678). Спробуйте ще раз.\n")

    api_id = int(api_id_val)

    api_hash = os.environ.get("TELETHON_API_HASH")
    while not api_hash:
        api_hash = input("Введіть ваш Telegram API HASH (32-значний рядок з my.telegram.org): ").strip()
        if not api_hash:
            print("❌ API HASH не може бути порожнім.")

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()

    print("\nОберіть спосіб авторизації:")
    print("  [1] Через QR-код (Рекомендовано — без SMS і кодів підтвердження)")
    print("  [2] За номером телефону (+380...)")
    choice = input("Ваш вибір [за замовчуванням 1]: ").strip() or "1"

    if choice == "1":
        print("\nГенерація QR-коду для входу...")
        qr_login = await client.qr_login()

        print("\n" + "=" * 65)
        print("ВІДСКАНУЙТЕ ЦЕЙ QR-КОД У ДОДАТКУ TELEGRAM НА ТЕЛЕФОНІ:")
        print("На телефоні: Налаштування -> Пристрої -> Підключити пристрій")
        print("=" * 65 + "\n")

        qr = qrcode.QRCode()
        qr.add_data(qr_login.url)
        qr.print_ascii(invert=True)

        print("\nОчікування сканування QR-коду у додатку Telegram...")
        try:
            await qr_login.wait(timeout=180)
        except TimeoutError:
            print("❌ Час очікування вичерпано. Запустіть скрипт знову.")
            await client.disconnect()
            return
        except SessionPasswordNeededError:
            pwd = input("\nВведіть ваш пароль двоетапної перевірки (2FA Cloud Password): ").strip()
            await client.sign_in(password=pwd)

    else:
        phone = input("\nВведіть ваш номер телефону Telegram (наприклад, +380971234567): ").strip()
        await client.start(phone=lambda: phone)

    me = await client.get_me()
    if me and getattr(me, "bot", False):
        print("\n❌ ПОМИЛКА: Сесія створена як БОТ, а не як користувач!")
        print("Telegram забороняє ботам читати публічні канали та їхню історію.")
        await client.disconnect()
        return

    session_str = client.session.save()
    await client.disconnect()

    user_title = f"{me.first_name} (@{me.username})" if me and me.username else (me.first_name if me else "Користувач")
    print("\n" + "=" * 65)
    print(f"✅ АВТОРИЗАЦІЯ УСПІШНА ({user_title})!")
    print("=" * 65)
    print(session_str)
    print("=" * 65)
    print("Скопіюйте цей рядок та збережіть у файлі .env:")
    print(f"TELETHON_STRING_SESSION={session_str}\n")


if __name__ == "__main__":
    asyncio.run(main())
