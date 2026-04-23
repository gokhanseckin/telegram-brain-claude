"""Onboarding FSM — /start and /tag commands.

Walks through the top 40 most-active chats one at a time, showing a preview
and an inline keyboard for tagging. Supports custom free-text tags via the
"+ Other" button, and a confirm-and-delete flow when tagging as "ignore".
Optionally collects a free-text note per chat before moving to the next.
"""

from __future__ import annotations

from datetime import datetime, timezone

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
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from tbc_bot.guards import is_owner
from tbc_common.config import settings
from tbc_common.db.models import (
    Chat,
    ChatSummary,
    Commitment,
    MessageUnderstanding,
    RadarAlert,
    RelationshipState,
)
from tbc_common.db.models import Message as TgMessage
from tbc_common.db.session import get_sessionmaker

log = structlog.get_logger(__name__)

router = Router(name="onboarding")

BASE_TAGS = ["client", "prospect", "colleague", "personal", "ignore", "other", "skip"]
RESERVED_TAGS = {"skip", "other"}


class OnboardingState(StatesGroup):
    tagging = State()
    entering_tag = State()
    confirm_ignore = State()
    noting = State()


def _tag_keyboard(custom_tags: list[str]) -> InlineKeyboardMarkup:
    seen: set[str] = set()
    ordered: list[str] = []
    for t in BASE_TAGS:
        if t not in seen:
            ordered.append(t)
            seen.add(t)
    # Insert custom tags before "other"/"skip" so the action buttons stay last.
    action_tail = [t for t in ordered if t in RESERVED_TAGS]
    head = [t for t in ordered if t not in RESERVED_TAGS]
    for t in custom_tags:
        if t and t not in seen:
            head.append(t)
            seen.add(t)
    final = head + action_tail

    def _label(tag: str) -> str:
        if tag == "other":
            return "+ Other"
        return tag.capitalize()

    buttons = [
        InlineKeyboardButton(text=_label(t), callback_data=f"tag:{t}") for t in final
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_ignore_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Yes — Ignore & Delete", callback_data="ignore:confirm"
                ),
                InlineKeyboardButton(
                    text="No — I'll tag", callback_data="ignore:cancel"
                ),
            ]
        ]
    )


def _load_chats(session: Session) -> list[Chat]:
    """Top 40 chats ordered by message count descending."""
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
    return list(session.scalars(stmt).all())


def _load_custom_tags(session: Session) -> list[str]:
    """Distinct chat tags already in the DB that aren't part of BASE_TAGS."""
    rows = session.execute(
        select(Chat.tag).where(Chat.tag.isnot(None)).distinct()
    ).all()
    base_set = set(BASE_TAGS)
    return sorted({r[0] for r in rows if r[0] and r[0] not in base_set})


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


def _normalize_tag(raw: str) -> str | None:
    cleaned = (raw or "").strip().lower()
    if not cleaned:
        return None
    if cleaned.startswith("/"):
        return None
    if cleaned in RESERVED_TAGS:
        return None
    # Keep it short and single-token-ish so it renders as a button.
    if len(cleaned) > 24:
        return None
    return cleaned


def _delete_chat_messages(session: Session, chat_id: int) -> int:
    """Hard-delete everything we've stored for `chat_id`.

    Order matters because of FK constraints (message_understanding → messages)
    and because several tables reference chat_id without a FK cascade.
    """
    deleted_msgs = session.execute(
        delete(MessageUnderstanding).where(MessageUnderstanding.chat_id == chat_id)
    )
    session.execute(delete(Commitment).where(Commitment.chat_id == chat_id))
    session.execute(delete(RadarAlert).where(RadarAlert.chat_id == chat_id))
    session.execute(
        delete(RelationshipState).where(RelationshipState.chat_id == chat_id)
    )
    session.execute(delete(ChatSummary).where(ChatSummary.chat_id == chat_id))
    result = session.execute(delete(TgMessage).where(TgMessage.chat_id == chat_id))
    return int(result.rowcount or 0)


def perform_ignore_and_delete(chat_id: int) -> int:
    """Mark a chat as ignored and hard-delete all its stored messages.

    Returns the count of messages deleted. Shared by the onboarding confirm
    flow and the /ignore command.
    """
    now = datetime.now(timezone.utc)
    sm = get_sessionmaker()
    with sm() as session:
        deleted = _delete_chat_messages(session, chat_id)
        chat = session.get(Chat, chat_id)
        if chat:
            chat.tag = "ignore"
            chat.tag_set_at = now
        session.commit()
    return deleted


async def _send_next_chat(trigger: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    chats: list[dict] = data.get("chats", [])
    idx: int = data.get("idx", 0)
    custom_tags: list[str] = data.get("custom_tags", [])

    target_msg: Message | None
    if isinstance(trigger, Message):
        target_msg = trigger
    else:
        target_msg = trigger.message  # type: ignore[assignment]

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
        f"*{idx + 1}/{len(chats)}* — {title}\n\n"
        f"Last messages:\n{preview}\n\n"
        "How would you tag this chat?"
    )

    await state.set_state(OnboardingState.tagging)

    if target_msg:
        await target_msg.answer(
            text, reply_markup=_tag_keyboard(custom_tags), parse_mode="Markdown"
        )


async def _start_onboarding(message: Message, state: FSMContext) -> None:
    if not is_owner(message):
        return

    sm = get_sessionmaker()
    with sm() as session:
        chats = _load_chats(session)
        custom_tags = _load_custom_tags(session)

    if not chats:
        await message.answer("No chats found yet. Make sure ingestion has run first.")
        return

    chats_data = [{"chat_id": c.chat_id, "title": c.title} for c in chats]
    await state.update_data(chats=chats_data, idx=0, custom_tags=custom_tags)
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
    await _start_onboarding(message, state)


async def _finalize_tag_and_advance(
    query_or_msg: Message | CallbackQuery,
    state: FSMContext,
    chosen_tag: str,
    chat_id: int,
) -> None:
    now = datetime.now(timezone.utc)
    sm = get_sessionmaker()
    with sm() as session:
        chat = session.get(Chat, chat_id)
        if chat:
            chat.tag = chosen_tag
            chat.tag_set_at = now
        session.commit()
    await state.update_data(pending_tag=chosen_tag, pending_chat_id=chat_id)
    target = (
        query_or_msg if isinstance(query_or_msg, Message) else query_or_msg.message
    )
    if target:
        await target.answer(
            "Optional: add a note about this chat (who they are, what matters). "
            "Send any text, or /skip to continue."
        )
    await state.set_state(OnboardingState.noting)


@router.callback_query(OnboardingState.tagging, F.data.startswith("tag:"))
async def on_tag_button(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return

    chosen_tag = (query.data or "tag:skip").split(":", 1)[1]
    data = await state.get_data()
    chats: list[dict] = data["chats"]
    idx: int = data["idx"]
    chat_data = chats[idx]
    chat_id = chat_data["chat_id"]

    if chosen_tag == "skip":
        await query.answer("Skipped.")
        await state.update_data(idx=idx + 1)
        await _send_next_chat(query, state)
        return

    if chosen_tag == "other":
        await query.answer()
        if query.message:
            await query.message.answer("Enter new tag:")
        await state.set_state(OnboardingState.entering_tag)
        return

    if chosen_tag == "ignore":
        await query.answer()
        if query.message:
            await query.message.answer(
                "Are you sure? Messages from this chat will be ignored and "
                "existing messages will be deleted.",
                reply_markup=_confirm_ignore_keyboard(),
            )
        await state.set_state(OnboardingState.confirm_ignore)
        return

    await query.answer(f"Tagged as {chosen_tag}.")
    await _finalize_tag_and_advance(query, state, chosen_tag, chat_id)


@router.message(OnboardingState.entering_tag)
async def on_enter_tag(message: Message, state: FSMContext) -> None:
    if not is_owner(message):
        return

    new_tag = _normalize_tag(message.text or "")
    if new_tag is None:
        await message.answer(
            "That tag isn't valid. Try a short word (letters/numbers), "
            "and avoid 'skip' / 'other' / commands."
        )
        return

    data = await state.get_data()
    chats: list[dict] = data["chats"]
    idx: int = data["idx"]
    chat_id = chats[idx]["chat_id"]
    custom_tags: list[str] = list(data.get("custom_tags", []))
    if new_tag not in custom_tags and new_tag not in BASE_TAGS:
        custom_tags.append(new_tag)
    await state.update_data(custom_tags=custom_tags)

    await message.answer(f"Tagged as {new_tag}.")
    await _finalize_tag_and_advance(message, state, new_tag, chat_id)


@router.callback_query(OnboardingState.confirm_ignore, F.data == "ignore:confirm")
async def on_ignore_confirm(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return

    data = await state.get_data()
    chats: list[dict] = data["chats"]
    idx: int = data["idx"]
    chat_id = chats[idx]["chat_id"]

    deleted = perform_ignore_and_delete(chat_id)
    await query.answer(f"Ignored. {deleted} messages deleted.")
    if query.message:
        await query.message.answer(f"Ignored and deleted {deleted} messages.")
    await state.update_data(idx=idx + 1)
    await _send_next_chat(query, state)


@router.callback_query(OnboardingState.confirm_ignore, F.data == "ignore:cancel")
async def on_ignore_cancel(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return
    await query.answer("Cancelled.")
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
