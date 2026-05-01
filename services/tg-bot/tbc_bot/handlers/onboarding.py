"""Onboarding FSM — /start and /tag commands.

Walks through the top 40 most-active chats one at a time, showing a preview
and an inline keyboard for tagging. Optionally collects a free-text note per
chat before moving to the next.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tbc_common.config import settings
from tbc_common.db.models import Chat
from tbc_common.db.models import Message as TgMessage
from tbc_common.db.session import get_sessionmaker
from tbc_common.db.tags import get_active_tags

from tbc_bot.guards import is_owner

log = structlog.get_logger(__name__)

router = Router(name="onboarding")


class OnboardingState(StatesGroup):
    tagging = State()
    noting = State()


def _tag_keyboard(tag_names: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=t.capitalize(), callback_data=f"tag:{t}")
        for t in tag_names
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _load_tag_names() -> list[str]:
    sm = get_sessionmaker()
    with sm() as session:
        tags = get_active_tags(session)
    return [t.name for t in tags] + ["skip"]


def _load_chats(session: Session, skip_tagged: bool = False) -> list[Chat]:
    """Top 40 chats ordered by message count descending.

    If skip_tagged is True, exclude chats that already have a tag.
    """
    subq = (
        select(
            TgMessage.chat_id,
            func.count(TgMessage.message_id).label("msg_count"),
        )
        .group_by(TgMessage.chat_id)
        .subquery()
    )
    stmt = (
        select(Chat)
        .join(subq, Chat.chat_id == subq.c.chat_id)
        .order_by(subq.c.msg_count.desc())
        .limit(40)
    )
    if skip_tagged:
        stmt = stmt.where(Chat.tag.is_(None))
    return list(session.scalars(stmt).all())


def _last_messages_preview(session: Session, chat_id: int) -> str:
    stmt = (
        select(TgMessage.text)
        .where(TgMessage.chat_id == chat_id, TgMessage.text.isnot(None))
        .order_by(TgMessage.sent_at.desc())
        .limit(3)
    )
    rows = list(session.scalars(stmt).all())
    rows.reverse()
    if not rows:
        return "(no messages)"
    return "\n".join(f"  • {(t or '')[:80]}" for t in rows)


async def _send_next_chat(trigger: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    chats: list[dict[str, Any]] = data.get("chats", [])
    idx: int = data.get("idx", 0)

    target_msg: Message | None = (
        trigger if isinstance(trigger, Message) else trigger.message  # type: ignore[assignment]
    )

    if idx >= len(chats):
        if target_msg:
            await target_msg.answer("Onboarding complete! I'll start processing your messages.")
        await state.clear()
        return

    chat_data = chats[idx]
    chat_id = chat_data["chat_id"]
    title = chat_data["title"] or f"chat_{chat_id}"

    sm = get_sessionmaker()
    with sm() as session:
        preview = _last_messages_preview(session, chat_id)

    text = (
        f"{idx + 1}/{len(chats)} — {title}\n\n"
        f"Last messages:\n{preview}\n\n"
        "How would you tag this chat?"
    )

    await state.set_state(OnboardingState.tagging)

    if target_msg:
        tag_names = _load_tag_names()
        await target_msg.answer(text, reply_markup=_tag_keyboard(tag_names), parse_mode=None)


async def _start_onboarding(
    message: Message, state: FSMContext, skip_tagged: bool = False
) -> None:
    if not is_owner(message):
        return

    sm = get_sessionmaker()
    with sm() as session:
        chats = _load_chats(session, skip_tagged=skip_tagged)

    if not chats:
        if skip_tagged:
            await message.answer("No untagged chats left. All your top chats are tagged.")
        else:
            await message.answer("No chats found yet. Make sure ingestion has run first.")
        return

    chats_data = [{"chat_id": c.chat_id, "title": c.title} for c in chats]
    await state.update_data(chats=chats_data, idx=0)
    await message.answer(
        f"Starting onboarding — {len(chats_data)} chats to tag. "
        "For each chat you'll see its name and recent messages. "
        "Tap a button to tag, or Skip to leave it untagged."
    )
    await _send_next_chat(message, state)


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await _start_onboarding(message, state)


@router.message(Command("tag"))
async def cmd_tag(message: Message, state: FSMContext) -> None:
    await _start_onboarding(message, state, skip_tagged=True)


@router.callback_query(OnboardingState.tagging, F.data.startswith("tag:"))
async def on_tag_button(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return

    chosen_tag = (query.data or "tag:skip").split(":", 1)[1]
    data = await state.get_data()
    chats: list[dict[str, Any]] = data["chats"]
    idx: int = data["idx"]
    chat_data = chats[idx]

    if chosen_tag != "skip":
        now = datetime.now(UTC)
        sm = get_sessionmaker()
        with sm() as session:
            chat = session.get(Chat, chat_data["chat_id"])
            if chat:
                chat.tag = chosen_tag
                chat.tag_set_at = now
                chat.tag_source = "manual"
                chat.tag_locked = True
                chat.tag_confidence = None
                chat.tag_reason = None
            session.commit()
        await state.update_data(pending_tag=chosen_tag, pending_chat_id=chat_data["chat_id"])
        await query.answer(f"Tagged as {chosen_tag}.")
        if query.message:
            await query.message.answer(
                "Optional: add a note about this chat (who they are, what matters). "
                "Send any text, or /skip to continue."
            )
        await state.set_state(OnboardingState.noting)
    else:
        await query.answer("Skipped.")
        await state.update_data(idx=idx + 1)
        await _send_next_chat(query, state)


@router.message(OnboardingState.noting)
async def on_note(message: Message, state: FSMContext) -> None:
    if not is_owner(message):
        return

    data = await state.get_data()
    note_text = (message.text or "").strip()
    chat_id = data.get("pending_chat_id")

    if note_text and note_text != "/skip" and chat_id:
        sm = get_sessionmaker()
        with sm() as session:
            chat = session.get(Chat, chat_id)
            if chat:
                chat.notes = note_text
            session.commit()

    idx: int = data["idx"]
    await state.update_data(idx=idx + 1)
    await _send_next_chat(message, state)
