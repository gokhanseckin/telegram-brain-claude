"""/retag — guided FSM for re-tagging a chat.

Three steps:
1. /retag → "Send chat name, @username, or #ref:"
2. User sends target → search; 0 → error, 1 → tag picker, many → candidate picker → tag picker
3. User picks tag → apply via the same executor the LLM router uses

Bypasses the LLM router entirely so disambiguation is always explicit.
"""

from __future__ import annotations

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
from sqlalchemy import select
from tbc_common.config import settings
from tbc_common.db.models import Tag
from tbc_common.db.session import get_sessionmaker

from tbc_bot.guards import is_owner
from tbc_bot.router.executors import _apply_retag_sync, _search_chat_sync

log = structlog.get_logger(__name__)

router = Router(name="retag")


class RetagCmdState(StatesGroup):
    waiting_target = State()
    picking_chat = State()
    picking_tag = State()


def _chat_picker_keyboard(pairs: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = []
    for chat_id, title in pairs[:10]:
        label = title if len(title) <= 30 else title[:27] + "..."
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label} ({chat_id})",
                    callback_data=f"retag_cmd:chat:{chat_id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="Cancel", callback_data="retag_cmd:cancel")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _tag_picker_keyboard(tag_names: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=t, callback_data=f"retag_cmd:tag:{t}")
        for t in tag_names
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append(
        [InlineKeyboardButton(text="Cancel", callback_data="retag_cmd:cancel")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _load_active_tag_names() -> list[str]:
    sm = get_sessionmaker()
    with sm() as session:
        return [
            t.name
            for t in session.scalars(
                select(Tag)
                .where(Tag.is_active.is_(True))
                .order_by(Tag.sort_order, Tag.name)
            ).all()
        ]


@router.message(Command("retag"))
async def cmd_retag(message: Message, state: FSMContext) -> None:
    if not is_owner(message):
        return
    await state.set_state(RetagCmdState.waiting_target)
    await message.answer(
        "Send chat name, @username, or #ref to retag.\n"
        "Send /cancel to abort."
    )


@router.message(RetagCmdState.waiting_target)
async def retag_got_target(message: Message, state: FSMContext) -> None:
    if not is_owner(message):
        return
    raw = (message.text or "").strip()
    if raw == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    if not raw:
        await message.answer("Empty target. Send a name, @username, or #ref:")
        return

    count, pairs = _search_chat_sync(raw)

    if count == 0:
        await state.clear()
        await message.answer(f"No chat matching '{raw}' found.")
        return

    if count == 1:
        chat_id, title = pairs[0]
        await state.update_data(chat_id=chat_id, title=title)
        await state.set_state(RetagCmdState.picking_tag)
        tag_names = _load_active_tag_names()
        await message.answer(
            f"Retagging '{title}' (chat {chat_id}). Pick a tag:",
            reply_markup=_tag_picker_keyboard(tag_names),
        )
        return

    # many — let user pick one
    await state.update_data(candidates=pairs)
    await state.set_state(RetagCmdState.picking_chat)
    await message.answer(
        f"Found {count} chats matching '{raw}'. Pick one:",
        reply_markup=_chat_picker_keyboard(pairs),
    )


@router.callback_query(F.data == "retag_cmd:cancel")
async def retag_cancel(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return
    await state.clear()
    await query.answer("Cancelled.")
    if query.message:
        await query.message.answer("Cancelled.")


@router.callback_query(RetagCmdState.picking_chat, F.data.startswith("retag_cmd:chat:"))
async def retag_picked_chat(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return
    chat_id = int((query.data or "").split(":", 2)[2])

    data = await state.get_data()
    candidates: list[tuple[int, str]] = data.get("candidates", [])
    title = next((t for cid, t in candidates if cid == chat_id), f"chat_{chat_id}")

    await state.update_data(chat_id=chat_id, title=title)
    await state.set_state(RetagCmdState.picking_tag)
    await query.answer()
    if query.message:
        tag_names = _load_active_tag_names()
        await query.message.answer(
            f"Retagging '{title}' (chat {chat_id}). Pick a tag:",
            reply_markup=_tag_picker_keyboard(tag_names),
        )


@router.callback_query(RetagCmdState.picking_tag, F.data.startswith("retag_cmd:tag:"))
async def retag_picked_tag(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return
    new_tag = (query.data or "").split(":", 2)[2]

    data = await state.get_data()
    chat_id: int = data["chat_id"]
    title: str = data.get("title", f"chat_{chat_id}")

    try:
        _apply_retag_sync(chat_id, new_tag)
    except Exception:
        log.exception("retag_cmd_apply_failed", chat_id=chat_id, new_tag=new_tag)
        await state.clear()
        await query.answer("Failed.")
        if query.message:
            await query.message.answer("Retag failed. See logs.")
        return

    await state.clear()
    await query.answer("Retagged.")
    if query.message:
        await query.message.answer(
            f"✓ Retagged '{title}' (chat {chat_id}) as {new_tag}."
        )
    log.info("retag_cmd_applied", chat_id=chat_id, new_tag=new_tag)
