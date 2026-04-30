"""Tag management FSM handlers — /newtag, /edittag, /listtags.

/newtag   — create a new user-defined tag (FSM: name → description → guidance)
/edittag  — edit or deactivate an existing tag (FSM: choose tag → choose field → edit)
/listtags — list all active tags (no FSM)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

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

log = structlog.get_logger(__name__)

router = Router(name="tags")

_NAME_RE = re.compile(r"^[a-z0-9_]{1,30}$")


# ---------------------------------------------------------------------------
# FSM state groups
# ---------------------------------------------------------------------------


class TagCreateState(StatesGroup):
    waiting_name = State()
    waiting_description = State()
    waiting_guidance = State()


class TagEditState(StatesGroup):
    choosing_tag = State()
    choosing_field = State()
    editing_description = State()
    editing_guidance = State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tag_select_keyboard(tag_names: list[str]) -> InlineKeyboardMarkup:
    """3-per-row inline keyboard with tag names for /edittag selection."""
    buttons = [
        InlineKeyboardButton(text=name, callback_data=f"edittag:select:{name}")
        for name in tag_names
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _field_keyboard(tag_name: str) -> InlineKeyboardMarkup:
    """Inline keyboard for choosing which field to edit."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Edit description",
                    callback_data=f"edittag:field:{tag_name}:description",
                ),
                InlineKeyboardButton(
                    text="Edit guidance",
                    callback_data=f"edittag:field:{tag_name}:guidance",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Deactivate",
                    callback_data=f"edittag:field:{tag_name}:deactivate",
                ),
                InlineKeyboardButton(
                    text="Cancel",
                    callback_data="edittag:cancel",
                ),
            ],
        ]
    )


def _confirm_deactivate_keyboard(tag_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Yes, deactivate",
                    callback_data=f"edittag:confirm_deactivate:{tag_name}",
                ),
                InlineKeyboardButton(
                    text="Cancel",
                    callback_data="edittag:cancel",
                ),
            ]
        ]
    )


def _load_active_tag_names() -> list[str]:
    sm = get_sessionmaker()
    with sm() as session:
        rows = list(
            session.scalars(
                select(Tag.name)
                .where(Tag.is_active.is_(True))
                .order_by(Tag.sort_order, Tag.name)
            ).all()
        )
    return rows


# ---------------------------------------------------------------------------
# /cancel — abort any active FSM flow
# ---------------------------------------------------------------------------


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if not is_owner(message):
        return
    current = await state.get_state()
    if current is None:
        await message.answer("Nothing to cancel.")
        return
    await state.clear()
    await message.answer("Cancelled.")


# ---------------------------------------------------------------------------
# /listtags
# ---------------------------------------------------------------------------


@router.message(Command("listtags"))
async def cmd_listtags(message: Message) -> None:
    if not is_owner(message):
        return

    sm = get_sessionmaker()
    with sm() as session:
        tags = list(
            session.scalars(
                select(Tag)
                .where(Tag.is_active.is_(True))
                .order_by(Tag.sort_order, Tag.name)
            ).all()
        )

    if not tags:
        await message.answer("No active tags found.")
        return

    lines = ["📋 Active Tags:"]
    for i, tag in enumerate(tags, 1):
        desc_line = f"{i}. {tag.name} — {tag.description}"
        lines.append(desc_line)
        if tag.analysis_guidance:
            lines.append(f"   AI: {tag.analysis_guidance}")

    await message.answer("\n".join(lines), parse_mode=None)


# ---------------------------------------------------------------------------
# /newtag — FSM
# ---------------------------------------------------------------------------


@router.message(Command("newtag"))
async def cmd_newtag(message: Message, state: FSMContext) -> None:
    if not is_owner(message):
        return
    await state.set_state(TagCreateState.waiting_name)
    await message.answer(
        "Send the tag name (lowercase, alphanumeric + underscore, max 30 chars):"
    )


@router.message(TagCreateState.waiting_name)
async def newtag_got_name(message: Message, state: FSMContext) -> None:
    if not is_owner(message):
        return

    raw = (message.text or "").strip()

    if raw == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    if not _NAME_RE.match(raw):
        await message.answer(
            "Invalid name. Use only lowercase letters, digits, and underscores (max 30 chars). Try again:"
        )
        return

    # Check uniqueness
    sm = get_sessionmaker()
    with sm() as session:
        existing = session.get(Tag, raw)

    if existing is not None:
        await message.answer(
            f"A tag named '{raw}' already exists. Choose a different name or /cancel:"
        )
        return

    await state.update_data(name=raw)
    await state.set_state(TagCreateState.waiting_description)
    await message.answer(f"Send a short description for '{raw}':")


@router.message(TagCreateState.waiting_description)
async def newtag_got_description(message: Message, state: FSMContext) -> None:
    if not is_owner(message):
        return

    raw = (message.text or "").strip()

    if raw == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    if not raw:
        await message.answer("Description cannot be empty. Send a short description:")
        return

    await state.update_data(description=raw)
    await state.set_state(TagCreateState.waiting_guidance)
    await message.answer(
        "Optional: analysis guidance for the AI (how to interpret signals for this tag). "
        "Send guidance or /skip:"
    )


@router.message(TagCreateState.waiting_guidance)
async def newtag_got_guidance(message: Message, state: FSMContext) -> None:
    if not is_owner(message):
        return

    raw = (message.text or "").strip()

    if raw == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    guidance: str | None = None if raw == "/skip" else (raw or None)

    data = await state.get_data()
    name: str = data["name"]
    description: str = data["description"]

    now = datetime.now(UTC)
    sm = get_sessionmaker()
    with sm() as session:
        tag = Tag(
            name=name,
            description=description,
            analysis_guidance=guidance,
            is_system=False,
            is_active=True,
            sort_order=100,
            created_at=now,
            updated_at=now,
        )
        session.add(tag)
        session.commit()

    await state.clear()
    await message.answer(f"✓ Tag created: {name} — {description}")
    log.info("tag.created", name=name, description=description, has_guidance=guidance is not None)


# ---------------------------------------------------------------------------
# /edittag — FSM
# ---------------------------------------------------------------------------


@router.message(Command("edittag"))
async def cmd_edittag(message: Message, state: FSMContext) -> None:
    if not is_owner(message):
        return

    tag_names = _load_active_tag_names()
    if not tag_names:
        await message.answer("No active tags to edit.")
        return

    await state.set_state(TagEditState.choosing_tag)
    await message.answer(
        "Select a tag to edit:",
        reply_markup=_tag_select_keyboard(tag_names),
    )


@router.callback_query(TagEditState.choosing_tag, F.data.startswith("edittag:select:"))
async def edittag_tag_selected(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return

    tag_name = (query.data or "").split(":", 2)[2]

    sm = get_sessionmaker()
    with sm() as session:
        tag = session.get(Tag, tag_name)

    if tag is None:
        await query.answer("Tag not found.")
        await state.clear()
        return

    await state.update_data(editing_tag=tag_name)
    await state.set_state(TagEditState.choosing_field)
    await query.answer()
    if query.message:
        await query.message.answer(
            f"Editing '{tag_name}'. What would you like to change?",
            reply_markup=_field_keyboard(tag_name),
        )


@router.callback_query(F.data == "edittag:cancel")
async def edittag_cancel_callback(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return
    await state.clear()
    await query.answer("Cancelled.")
    if query.message:
        await query.message.answer("Cancelled.")


@router.callback_query(TagEditState.choosing_field, F.data.startswith("edittag:field:"))
async def edittag_field_chosen(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return

    # callback_data format: edittag:field:<tag_name>:<field>
    parts = (query.data or "").split(":", 3)
    if len(parts) < 4:
        await query.answer("Malformed callback.")
        return

    tag_name = parts[2]
    field = parts[3]

    sm = get_sessionmaker()
    with sm() as session:
        tag = session.get(Tag, tag_name)

    if tag is None:
        await query.answer("Tag not found.")
        await state.clear()
        return

    await query.answer()

    if field == "description":
        await state.update_data(editing_tag=tag_name, editing_field="description")
        await state.set_state(TagEditState.editing_description)
        if query.message:
            await query.message.answer(
                f"Current: '{tag.description}'\nSend the new description:"
            )

    elif field == "guidance":
        current = tag.analysis_guidance or "(none)"
        await state.update_data(editing_tag=tag_name, editing_field="guidance")
        await state.set_state(TagEditState.editing_guidance)
        if query.message:
            await query.message.answer(
                f"Current: '{current}'\nSend new guidance or /skip to clear:"
            )

    elif field == "deactivate":
        if query.message:
            await query.message.answer(
                f"This will hide '{tag_name}' from tagging and prompts. "
                "Existing chats keep their tag. Confirm?",
                reply_markup=_confirm_deactivate_keyboard(tag_name),
            )

    else:
        await query.message.answer("Unknown field.") if query.message else None
        await state.clear()


@router.callback_query(TagEditState.editing_description)
async def edittag_cancel_description_callback(query: CallbackQuery, state: FSMContext) -> None:
    """Catch any stray callbacks while waiting for description text input."""
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return
    if (query.data or "") == "edittag:cancel":
        await state.clear()
        await query.answer("Cancelled.")
        if query.message:
            await query.message.answer("Cancelled.")
    else:
        await query.answer()


@router.message(TagEditState.editing_description)
async def edittag_got_new_description(message: Message, state: FSMContext) -> None:
    if not is_owner(message):
        return

    raw = (message.text or "").strip()

    if raw == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    if not raw:
        await message.answer("Description cannot be empty. Send a new description:")
        return

    data = await state.get_data()
    tag_name: str = data["editing_tag"]

    now = datetime.now(UTC)
    sm = get_sessionmaker()
    with sm() as session:
        tag = session.get(Tag, tag_name)
        if tag is None:
            await message.answer("Tag not found. Cancelled.")
            await state.clear()
            return
        tag.description = raw
        tag.updated_at = now
        session.commit()

    await state.clear()
    await message.answer(f"✓ Description updated for '{tag_name}'.")
    log.info("tag.updated", name=tag_name, field="description")


@router.callback_query(TagEditState.editing_guidance)
async def edittag_cancel_guidance_callback(query: CallbackQuery, state: FSMContext) -> None:
    """Catch any stray callbacks while waiting for guidance text input."""
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return
    if (query.data or "") == "edittag:cancel":
        await state.clear()
        await query.answer("Cancelled.")
        if query.message:
            await query.message.answer("Cancelled.")
    else:
        await query.answer()


@router.message(TagEditState.editing_guidance)
async def edittag_got_new_guidance(message: Message, state: FSMContext) -> None:
    if not is_owner(message):
        return

    raw = (message.text or "").strip()

    if raw == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    guidance: str | None = None if raw == "/skip" else (raw or None)

    data = await state.get_data()
    tag_name: str = data["editing_tag"]

    now = datetime.now(UTC)
    sm = get_sessionmaker()
    with sm() as session:
        tag = session.get(Tag, tag_name)
        if tag is None:
            await message.answer("Tag not found. Cancelled.")
            await state.clear()
            return
        tag.analysis_guidance = guidance
        tag.updated_at = now
        session.commit()

    await state.clear()
    action = "cleared" if guidance is None else "updated"
    await message.answer(f"✓ Analysis guidance {action} for '{tag_name}'.")
    log.info("tag.updated", name=tag_name, field="guidance")


@router.callback_query(F.data.startswith("edittag:confirm_deactivate:"))
async def edittag_confirm_deactivate(query: CallbackQuery, state: FSMContext) -> None:
    if query.from_user.id != settings.tg_owner_user_id:
        await query.answer()
        return

    tag_name = (query.data or "").split(":", 2)[2]

    now = datetime.now(UTC)
    sm = get_sessionmaker()
    with sm() as session:
        tag = session.get(Tag, tag_name)
        if tag is None:
            await query.answer("Tag not found.")
            await state.clear()
            return
        tag.is_active = False
        tag.updated_at = now
        session.commit()

    await state.clear()
    await query.answer("Deactivated.")
    if query.message:
        await query.message.answer(
            f"✓ Tag '{tag_name}' deactivated. Existing chats keep their tag."
        )
    log.info("tag.deactivated", name=tag_name)
