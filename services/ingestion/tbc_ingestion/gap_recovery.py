"""Gap recovery: backfill messages for known chats on service startup.

For every non-ignored chat already in the `chats` table, we fetch messages
that arrived while the service was offline and store them so the DB has
no gaps before live handlers take over.

See docs/mvp-spec.md §3 (gap recovery) and §9 (flood-wait handling).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from sqlalchemy import select
from tbc_common.db.models import Chat, Message, User
from tbc_common.db.session import get_sessionmaker
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import Message as TgMessage

from .handlers import _make_json_safe

log = structlog.get_logger(__name__)

_PAGE_LIMIT = 100
_PAGE_SLEEP_SECONDS = 1.0


async def run_gap_recovery(client: TelegramClient) -> None:
    """Backfill missing messages for all non-ignored known chats."""
    session_factory = get_sessionmaker()
    with session_factory() as session:
        stmt = select(Chat).where(
            ((Chat.tag != "ignore") | (Chat.tag.is_(None)))
            & (Chat.type != "channel")
            & ~((Chat.type == "supergroup") & (Chat.username.is_not(None)))
        )
        chats = list(session.scalars(stmt))

    if not chats:
        log.info("gap_recovery_no_chats")
        return

    log.info("gap_recovery_starting", chat_count=len(chats))

    for chat in chats:
        try:
            await _recover_chat(client, chat.chat_id)
        except Exception:
            log.exception("gap_recovery_chat_failed", chat_id=chat.chat_id)

    log.info("gap_recovery_complete")


async def _recover_chat(client: TelegramClient, chat_id: int) -> None:
    """Fetch and store all messages newer than what we already have."""
    session_factory = get_sessionmaker()

    # Find the highest message_id already stored for this chat.
    with session_factory() as session:
        from sqlalchemy import func as sa_func

        result = session.execute(
            select(sa_func.max(Message.message_id)).where(
                Message.chat_id == chat_id
            )
        ).scalar_one_or_none()
        min_id: int = result if result is not None else 0

    log.debug("gap_recovery_chat", chat_id=chat_id, min_id=min_id)

    total = 0
    while True:
        try:
            messages = await client.get_messages(
                chat_id,
                limit=_PAGE_LIMIT,
                min_id=min_id,
            )
        except FloodWaitError as e:
            log.warning(
                "gap_recovery_flood_wait",
                chat_id=chat_id,
                wait_seconds=e.seconds,
            )
            await asyncio.sleep(e.seconds)
            continue  # retry the same page
        except Exception:
            log.exception("gap_recovery_fetch_error", chat_id=chat_id)
            break

        if not messages:
            break

        await _store_messages(client, chat_id, list(messages))
        total += len(messages)

        # Advance min_id to the highest id we just fetched.
        new_min = max(m.id for m in messages)
        if new_min <= min_id:
            # No progress — shouldn't happen, but guard against infinite loop.
            break
        min_id = new_min

        if len(messages) < _PAGE_LIMIT:
            # Last page.
            break

        await asyncio.sleep(_PAGE_SLEEP_SECONDS)

    if total:
        log.info("gap_recovery_chat_done", chat_id=chat_id, messages_added=total)


async def _store_messages(
    client: TelegramClient, chat_id: int, messages: list[Any]
) -> None:
    """Persist a batch of Telethon messages.

    Two-phase: first resolve and commit any new sender Users so their rows exist
    before Messages reference them via FK; then insert Messages in a second
    session, falling back to sender_id=NULL for senders that couldn't be
    resolved. This avoids mid-loop FK races under autoflush.
    """
    tg_messages = [m for m in messages if isinstance(m, TgMessage)]
    if not tg_messages:
        return

    sender_ids: set[int] = {m.sender_id for m in tg_messages if m.sender_id is not None}

    session_factory = get_sessionmaker()

    # Phase 1: resolve + insert any missing Users. Commit before touching Messages.
    resolved: set[int] = set()
    if sender_ids:
        with session_factory() as session:
            existing = set(
                session.scalars(
                    select(User.user_id).where(User.user_id.in_(sender_ids))
                )
            )
            resolved |= existing
            for sid in sender_ids - existing:
                try:
                    sender = await client.get_entity(sid)
                    session.add(
                        User(
                            user_id=sid,
                            first_name=getattr(sender, "first_name", None),
                            last_name=getattr(sender, "last_name", None),
                            username=getattr(sender, "username", None),
                            is_self=getattr(sender, "is_self", False),
                        )
                    )
                    resolved.add(sid)
                except Exception:
                    log.debug("could_not_resolve_sender", sender_id=sid, chat_id=chat_id)
            session.commit()

    # Phase 2: insert Messages, nulling sender_id for any still-unresolved sender.
    with session_factory() as session:
        for msg in tg_messages:
            if session.get(Message, (chat_id, msg.id)) is not None:
                continue

            sender_id: int | None = None
            if msg.sender_id is not None and msg.sender_id in resolved:
                sender_id = msg.sender_id

            reply_to: int | None = None
            if hasattr(msg, "reply_to") and msg.reply_to is not None:
                reply_to = getattr(msg.reply_to, "reply_to_msg_id", None)

            session.add(
                Message(
                    message_id=msg.id,
                    chat_id=chat_id,
                    sender_id=sender_id,
                    sent_at=msg.date,
                    text=msg.message,
                    reply_to_id=reply_to,
                    edited_at=getattr(msg, "edit_date", None),
                    raw=_make_json_safe(msg.to_dict()),
                )
            )

        session.commit()
