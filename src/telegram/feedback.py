"""Feedback repository and Telegram handlers for user feedback and admin replies."""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

logger = logging.getLogger(__name__)


class FeedbackRepository:
    """Store feedback tickets and map admin group messages to user chats."""

    def __init__(self, db_path: str = "data/subscribers.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS feedback_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER,
                    user_chat_id INTEGER NOT NULL,
                    user_message_id INTEGER NOT NULL,
                    admin_message_id INTEGER NOT NULL UNIQUE,
                    created_at_utc TEXT NOT NULL,
                    status TEXT DEFAULT 'open'
                )
            """)
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_feedback_admin_msg
                ON feedback_tickets(admin_message_id)
            """)
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_feedback_user_chat
                ON feedback_tickets(user_chat_id)
            """)

    def create_ticket(self, user_chat_id: int, user_message_id: int, admin_message_id: int) -> int:
        now = datetime.now(UTC).isoformat()
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO feedback_tickets(user_chat_id, user_message_id, admin_message_id, created_at_utc)
                VALUES (?, ?, ?, ?)
                """,
                (user_chat_id, user_message_id, admin_message_id, now),
            )
            tid = cursor.lastrowid
            self.conn.execute(
                "UPDATE feedback_tickets SET ticket_id = ? WHERE id = ?",
                (tid, tid),
            )
            return tid

    def link_additional_admin_message(
        self,
        ticket_id: int,
        user_chat_id: int,
        user_message_id: int,
        admin_message_id: int,
    ) -> None:
        """Link follow-up admin messages to the same ticket context."""
        now = datetime.now(UTC).isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO feedback_tickets(ticket_id, user_chat_id, user_message_id, admin_message_id, created_at_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ticket_id, user_chat_id, user_message_id, admin_message_id, now),
            )

    def get_ticket_by_admin_message(self, admin_message_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM feedback_tickets WHERE admin_message_id = ?",
            (admin_message_id,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        if not data.get("ticket_id"):
            data["ticket_id"] = data["id"]
        return data

    def last_ticket_time(self, user_chat_id: int) -> datetime | None:
        row = self.conn.execute(
            "SELECT created_at_utc FROM feedback_tickets WHERE user_chat_id = ? ORDER BY id DESC LIMIT 1",
            (user_chat_id,),
        ).fetchone()
        if not row:
            return None
        try:
            return datetime.fromisoformat(row["created_at_utc"]).astimezone(UTC)
        except (ValueError, TypeError):
            return None

    def delete_user_tickets(self, user_chat_id: int) -> int:
        with self.conn:
            return self.conn.execute(
                "DELETE FROM feedback_tickets WHERE user_chat_id = ?",
                (user_chat_id,),
            ).rowcount

    def close(self) -> None:
        self.conn.close()


class FeedbackStates(StatesGroup):
    waiting_for_feedback = State()


def register_feedback_handlers(
    dp: Dispatcher,
    repo: FeedbackRepository,
    feedback_chat_id: int | None = None,
    admin_ids: list[int] | None = None,
) -> None:
    """Register /feedback, /cancel and admin response handlers on Dispatcher."""
    admin_ids_set = set(admin_ids or [])

    @dp.message(Command("feedback"))
    async def cmd_feedback(message: Message, state: FSMContext):
        # 1. Rate limit check (60s cooldown per user)
        last_time = repo.last_ticket_time(message.chat.id)
        if last_time:
            elapsed = (datetime.now(UTC) - last_time).total_seconds()
            if elapsed < 60.0:
                remaining = int(60.0 - elapsed)
                await message.answer(
                    f"⏳ Будь ласка, зачекайте ще {remaining} с перед відправкою нового звернення.",
                    parse_mode="Markdown",
                )
                return

        await state.set_state(FeedbackStates.waiting_for_feedback)
        text = (
            "💬 **Напишіть, що варто покращити, або повідомте про помилку.** Можна додати скриншот.\n"
            "Якщо знадобиться уточнення, відповідь розробника надійде сюди.\n\n"
            "Скасувати: /cancel\n\n"
            "_Це форма технічного зворотного звʼязку, а не канал екстреної допомоги._"
        )
        await message.answer(text, parse_mode="Markdown")

    @dp.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext):
        current_state = await state.get_state()
        if current_state is None:
            await message.answer("Немає активних дій для скасування.")
            return
        await state.clear()
        await message.answer("❌ Відправку відгуку скасовано.")

    @dp.message(FeedbackStates.waiting_for_feedback)
    async def process_feedback_message(message: Message, state: FSMContext, bot: Bot):
        # Allow cancelling with plain text
        if message.text and message.text.strip().lower() in ("/cancel", "cancel", "відміна", "скасувати"):
            await state.clear()
            await message.answer("❌ Відправку відгуку скасовано.")
            return

        if not feedback_chat_id:
            logger.warning("FEEDBACK_CHAT_ID не налаштовано. Неможливо доставити звернення.")
            await message.answer(
                "⚠️ Сервіс зворотного звʼязку тимчасово ненастроєний. Спробуйте пізніше."
            )
            await state.clear()
            return

        # Check content: must have text or photo
        if not message.text and not message.photo:
            await message.answer(
                "Будь ласка, надішліть текст або скриншот із описом проблеми.\nСкасувати: /cancel"
            )
            return

        try:
            # 1. Format metadata for admin chat
            user_name = message.from_user.full_name if message.from_user else "Користувач"
            user_handle = f" (@{message.from_user.username})" if message.from_user and message.from_user.username else ""
            header = f"💬 **Звернення** від {user_name}{user_handle} (`{message.chat.id}`):\n\n"

            # 2. Forward / deliver to admin chat
            if message.text:
                admin_msg = await bot.send_message(
                    chat_id=feedback_chat_id,
                    text=f"{header}{message.text}",
                    parse_mode="Markdown",
                )
            else:
                caption = header
                if message.caption:
                    caption += message.caption
                admin_msg = await bot.send_photo(
                    chat_id=feedback_chat_id,
                    photo=message.photo[-1].file_id,
                    caption=caption,
                    parse_mode="Markdown",
                )

            # 3. Save ticket
            ticket_id = repo.create_ticket(
                user_chat_id=message.chat.id,
                user_message_id=message.message_id,
                admin_message_id=admin_msg.message_id,
            )

            await message.answer(
                f"✅ **Дякуємо за відгук!** Ваше звернення #{ticket_id} передано розробнику.\n"
                "Якщо знадобиться уточнення, відповідь надійде в цей чат.",
                parse_mode="Markdown",
            )
            await state.clear()
        except Exception:
            logger.exception("Помилка відправки звернення в адмін-чат")
            await message.answer(
                "⚠️ Не вдалося доставити відгук через технічну помилку. Будь ласка, спробуйте пізніше."
            )
            await state.clear()

    if feedback_chat_id:
        @dp.message(F.chat.id == feedback_chat_id, F.reply_to_message)
        async def handle_admin_reply(message: Message, bot: Bot):
            # Verify admin permission if admin_ids is specified
            if admin_ids_set and message.from_user and message.from_user.id not in admin_ids_set:
                logger.warning(
                    "Користувач %s не має прав для відповіді на звернення (не в FEEDBACK_ADMIN_IDS)",
                    message.from_user.id,
                )
                return

            reply_msg_id = message.reply_to_message.message_id
            ticket = repo.get_ticket_by_admin_message(reply_msg_id)
            if not ticket:
                return

            user_chat_id = ticket["user_chat_id"]
            reply_prefix = "💬 **Відповідь розробника:**\n\n"

            try:
                if message.text:
                    await bot.send_message(
                        chat_id=user_chat_id,
                        text=f"{reply_prefix}{message.text}",
                        parse_mode="Markdown",
                    )
                elif message.photo:
                    cap = f"{reply_prefix}{message.caption or ''}"
                    await bot.send_photo(
                        chat_id=user_chat_id,
                        photo=message.photo[-1].file_id,
                        caption=cap,
                        parse_mode="Markdown",
                    )
                else:
                    await bot.copy_message(
                        chat_id=user_chat_id,
                        from_chat_id=message.chat.id,
                        message_id=message.message_id,
                    )

                repo.link_additional_admin_message(
                    ticket_id=ticket["ticket_id"],
                    user_chat_id=user_chat_id,
                    user_message_id=ticket["user_message_id"],
                    admin_message_id=message.message_id,
                )

                await message.reply("✅ Відповідь успішно доставлено користувачеві.")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не вдалося доставити відповідь користувачеві %s: %s", user_chat_id, exc)
                await message.reply(
                    f"⚠️ Не вдалося доставити відповідь користувачеві: {exc}.\n"
                    "Можливо, користувач заблокував бота або видалив чат."
                )
