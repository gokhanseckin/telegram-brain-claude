"""Telethon event handlers: NewMessage, MessageEdited, MessageDeleted.

Each handler upserts the relevant DB rows and preserves history as
described in docs/mvp-spec.md §3 and §9.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy.orm import Session
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

from tbc_common.db.models import Chat, Message, User
from tbc_common.db.session import get_sessionmaker

from .pause import is_paused

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chat_type(entity: Any) -> str:
    """Map a Telethon entity to our chat type string."""
    from telethon.tl.types import (
        Channel,
        Chat as TgChat,
        User as TgUser,
    )

    if isinstance(entity, TgUser):
        return "private"
    if isinstance(entity, TgChat):
        return "group"
    if isinstance(entity, Channel):
        return "supergroup" if entity.megagroup else "channel"
    return "group"


def _upsert_user(session: Session, sender: Any) -> None:
    """Insert or update a users row from a Telethon User entity."""
    if sender is None:
        return
    existing = session.get(User, sender.id)
    if existing is None:
        session.add(
            User(
                user_id=sender.id,
                first_name=getattr(sender, "first_name", None),
                last_name=getattr(sender, "last_name", None),
                username=getattr(sender, "username", None),
                is_self=getattr(sender, "is_self", False),
            )
        )
    else:
        existing.first_name = getattr(sender, "first_name", None)
        existing.last_name = getattr(sender, "last_name", None)
        existing.username = getattr(sender, "username", None)


def _upsert_chat(session: Session, chat_id: int, entity: Any) -> None:
    """Insert a chats row if it doesn't already exist."""
    existing = session.get(Chat, chat_id)
    if existing is None:
        chat_type = _chat_type(entity) if entity is not None else "group"
        session.add(
            Chat(
                chat_id=chat_id,
                type=chat_type,
                title=getattr(entity, "title", None) or getattr(entity, "first_name", None),
                username=getattr(entity, "username", None),
            )
        )


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------


def register_handlers(client: TelegramClient) -> None:
    """Attach all three event handlers to the client."""

    @client.on(events.NewMessage)
    async def on_new_message(event: events.NewMessage.Event) -> None:
        if is_paused():
            return
        try:
            await _handle_new_message(event)
        except FloodWaitError as e:
            log.warning("flood_wait_new_message", wait_seconds=e.seconds)
            await asyncio.sleep(e.seconds)
        except Exception:
            log.exception("error_in_new_message_handler")

    @client.on(events.MessageEdited)
    async def on_message_edited(event: events.MessageEdited.Event) -> None:
        if is_paused():
            return
        try:
            await _handle_message_edited(event)
        except Exception:
            log.exception("error_in_message_edited_handler")

    @client.on(events.MessageDeleted)
    async def on_message_deleted(event: events.MessageDeleted.Event) -> None:
        if is_paused():
            return
        try:
            await _handle_message_deleted(event)
        except Exception:
            log.exception("error_in_message_deleted_handler")

    log.info("event_handlers_registered")


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------


async def _handle_new_message(event: events.NewMessage.Event) -> None:
    """Upsert user, upsert chat, insert message row."""
    msg = event.message
    chat_id: int = event.chat_id

    sender = await event.get_sender()
    chat_entity = await event.get_chat()

    Session = get_sessionmaker()
    with Session() as session:
        _upsert_user(session, sender)
        _upsert_chat(session, chat_id, chat_entity)

        sender_id = sender.id if sender is not None else None

        # Check if message already exists (e.g. from gap recovery)
        existing = session.get(Message, (chat_id, msg.id))
        if existing is None:
            session.add(
                Message(
                    message_id=msg.id,
                    chat_id=chat_id,
                    sender_id=sender_id,
                    sent_at=msg.date,
                    text=msg.message,
                    reply_to_id=msg.reply_to_msg_id if hasattr(msg, "reply_to_msg_id") else None,
                    raw=msg.to_dict(),
                )
            )
        session.commit()

    log.debug("new_message_stored", chat_id=chat_id, message_id=msg.id)


async def _handle_message_edited(event: events.MessageEdited.Event) -> None:
    """Update text and edited_at; append old text to raw['edit_history']."""
    msg = event.message
    chat_id: int = event.chat_id

    Session = get_sessionmaker()
    with Session() as session:
        existing = session.get(Message, (chat_id, msg.id))
        if existing is None:
            # We haven't stored this message yet — treat like a new message.
            sender = await event.get_sender()
            chat_entity = await event.get_chat()
            _upsert_user(session, sender)
            _upsert_chat(session, chat_id, chat_entity)
            sender_id = sender.id if sender is not None else None
            session.add(
                Message(
                    message_id=msg.id,
                    chat_id=chat_id,
                    sender_id=sender_id,
                    sent_at=msg.date,
                    text=msg.message,
                    reply_to_id=getattr(msg, "reply_to_msg_id", None),
                    edited_at=msg.edit_date,
                    raw=msg.to_dict(),
                )
            )
        else:
            # Append old text to edit_history inside raw; never overwrite the
            # entire raw blob so we preserve the original Telegram object.
            raw: dict[str, Any] = dict(existing.raw)
            edit_history: list[dict[str, Any]] = raw.get("edit_history", [])
            edit_history.append(
                {
                    "text": existing.text,
                    "edited_at": existing.edited_at.isoformat()
                    if existing.edited_at
                    else None,
                    "replaced_at": datetime.now(tz=timezone.utc).isoformat(),
                }
            )
            raw["edit_history"] = edit_history
            existing.raw = raw
            existing.text = msg.message
            existing.edited_at = msg.edit_date

        session.commit()

    log.debug("message_edited_stored", chat_id=chat_id, message_id=msg.id)


async def _handle_message_deleted(event: events.MessageDeleted.Event) -> None:
    """Soft-delete: set deleted_at = NOW(). Never hard-delete."""
    # MessageDeleted carries deleted_ids (list of ints) and optionally chat_id.
    # channel_id is populated for channel/supergroup deletes; for private chats
    # it may be None (Telegram doesn't always send it for PM deletes).
    chat_id = getattr(event, "chat_id", None)
    deleted_ids = event.deleted_ids  # list[int]

    if not deleted_ids:
        return

    now = datetime.now(tz=timezone.utc)
    Session = get_sessionmaker()
    with Session() as session:
        for msg_id in deleted_ids:
            if chat_id is not None:
                existing = session.get(Message, (chat_id, msg_id))
                if existing is not None and existing.deleted_at is None:
                    existing.deleted_at = now
            # If chat_id is None we can't reliably locate the row; skip.
        session.commit()

    log.debug(
        "messages_soft_deleted",
        chat_id=chat_id,
        count=len(deleted_ids),
    )
