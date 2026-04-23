"""Simple command handlers: /ignore, /brief, /weekly, /search, /pause, /resume, /status."""

from __future__ import annotations

import os
import re
from pathlib import Path

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select, text

from tbc_bot.guards import is_owner
from tbc_bot.handlers.onboarding import perform_ignore_and_delete
from tbc_common.config import settings
from tbc_common.db.models import Chat
from tbc_common.db.models import Message as TgMessage
from tbc_common.db.models import MessageUnderstanding
from tbc_common.db.session import get_sessionmaker

log = structlog.get_logger(__name__)

router = Router(name="commands")

TRIGGER_BRIEF = Path("/tmp/tbc_trigger_brief")
TRIGGER_WEEKLY = Path("/tmp/tbc_trigger_weekly")
PAUSE_FILE = Path("/tmp/tbc_pause")


class IgnoreCmdState(StatesGroup):
    confirm = State()


def _ignore_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Yes — Ignore & Delete", callback_data="ignorecmd:confirm"
                ),
                InlineKeyboardButton(
                    text="No — Cancel", callback_data="ignorecmd:cancel"
                ),
            ]
        ]
    )


@router.message(Command("ignore"))
async def cmd_ignore(message: Message, state: FSMContext) -> None:
    if not is_owner(message):
        return

    args = (message.text or "").split(maxsplit=1)
    chat_name = args[1].strip() if len(args) > 1 else None

    if not chat_name:
        await message.answer(
            "To ignore a chat, use: /ignore ChatName\n"
            "(Direct chat context is not available in polling mode.)"
        )
        return

    sm = get_sessionmaker()
    with sm() as session:
        stmt = select(Chat).where(Chat.title.ilike(f"%{chat_name}%")).limit(1)
        chat = session.scalars(stmt).first()
        if chat is None:
            await message.answer(f"No chat found matching '{chat_name}'.")
            return
        chat_id = chat.chat_id
        chat_title = chat.title

    await state.update_data(pending_ignore_chat_id=chat_id, pending_ignore_title=chat_title)
    await state.set_state(IgnoreCmdState.confirm)
    await message.answer(
        f"Are you sure? Messages from '{chat_title}' will be ignored and "
        "existing messages will be deleted.",
        reply_markup=_ignore_confirm_keyboard(),
    )


@router.callback_query(IgnoreCmdState.confirm, F.data == "ignorecmd:confirm")
async def on_ignorecmd_confirm(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return
    data = await state.get_data()
    chat_id = data.get("pending_ignore_chat_id")
    title = data.get("pending_ignore_title") or f"chat_{chat_id}"
    await state.clear()
    if chat_id is None:
        await query.answer()
        return
    deleted = perform_ignore_and_delete(int(chat_id))
    await query.answer()
    if query.message:
        await query.message.answer(
            f"Ignored '{title}'. {deleted} messages deleted."
        )


@router.callback_query(IgnoreCmdState.confirm, F.data == "ignorecmd:cancel")
async def on_ignorecmd_cancel(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return
    await state.clear()
    await query.answer("Cancelled.")
    if query.message:
        await query.message.answer("Cancelled — nothing changed.")


@router.message(Command("brief"))
async def cmd_brief(message: Message) -> None:
    if not is_owner(message):
        return
    TRIGGER_BRIEF.touch()
    await message.answer("Brief generation triggered.")


@router.message(Command("weekly"))
async def cmd_weekly(message: Message) -> None:
    if not is_owner(message):
        return
    TRIGGER_WEEKLY.touch()
    await message.answer("Weekly review triggered.")


@router.message(Command("search"))
async def cmd_search(message: Message) -> None:
    if not is_owner(message):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Usage: /search <query>")
        return

    query = parts[1].strip()
    sm = get_sessionmaker()
    with sm() as session:
        stmt = (
            select(TgMessage.text, Chat.title)
            .join(Chat, Chat.chat_id == TgMessage.chat_id)
            .where(TgMessage.text.ilike(f"%{query}%"))
            .order_by(TgMessage.sent_at.desc())
            .limit(5)
        )
        rows = session.execute(stmt).all()

    if not rows:
        await message.answer("No messages found.")
        return

    lines = []
    for i, (msg_text, chat_title) in enumerate(rows, 1):
        snippet = (msg_text or "")[:120]
        lines.append(f"{i}. [{chat_title}] {snippet}")

    await message.answer("\n\n".join(lines))


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    if not is_owner(message):
        return
    PAUSE_FILE.touch()
    await message.answer("Ingestion paused.")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    if not is_owner(message):
        return
    try:
        PAUSE_FILE.unlink()
    except FileNotFoundError:
        pass
    await message.answer("Ingestion resumed.")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not is_owner(message):
        return

    sm = get_sessionmaker()
    with sm() as session:
        total_messages = session.scalar(select(func.count()).select_from(TgMessage)) or 0
        total_understood = (
            session.scalar(select(func.count()).select_from(MessageUnderstanding)) or 0
        )
        unprocessed = total_messages - total_understood

        last_msg_ts = session.scalar(
            select(func.max(TgMessage.sent_at))
        )
        last_understanding_ts = session.scalar(
            select(func.max(MessageUnderstanding.processed_at))
        )

    paused = PAUSE_FILE.exists()
    pause_status = " (PAUSED)" if paused else ""

    lines = [
        f"Status{pause_status}",
        f"Total messages: {total_messages:,}",
        f"Understood: {total_understood:,}",
        f"Unprocessed: {unprocessed:,}",
        f"Last message: {last_msg_ts.isoformat() if last_msg_ts else 'N/A'}",
        f"Last understanding: {last_understanding_ts.isoformat() if last_understanding_ts else 'N/A'}",
    ]
    await message.answer("\n".join(lines))
