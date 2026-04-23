"""Initial 30-day backfill on first deploy.

Enumerates all Telegram dialogs, excludes broadcast channels and public
supergroups, and pages back through messages sent in the last 30 days for
each remaining dialog. Runs at most once per install — guarded by the
`service_state.initial_backfill_done_at` column.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import structlog
from sqlalchemy import select
from telethon import TelegramClient
from telethon.errors import FloodWaitError

from tbc_common.config import settings
from tbc_common.db.models import ServiceState
from tbc_common.db.session import get_sessionmaker

from .gap_recovery import _PAGE_SLEEP_SECONDS, _store_messages
from .handlers import _is_excluded_chat, _upsert_chat, _upsert_user

log = structlog.get_logger(__name__)

BACKFILL_WINDOW_DAYS = 30
_DIALOG_FETCH_LIMIT = None  # iterate all dialogs


def _load_state() -> ServiceState:
    Session = get_sessionmaker()
    with Session() as session:
        state = session.get(ServiceState, 1)
        if state is None:
            state = ServiceState(id=1)
            session.add(state)
            session.commit()
            session.refresh(state)
        session.expunge(state)
        return state


def _mark_started() -> None:
    Session = get_sessionmaker()
    with Session() as session:
        state = session.get(ServiceState, 1)
        if state is None:
            state = ServiceState(id=1)
            session.add(state)
        state.initial_backfill_started_at = datetime.now(timezone.utc)
        session.commit()


def _mark_done() -> None:
    Session = get_sessionmaker()
    with Session() as session:
        state = session.get(ServiceState, 1)
        if state is None:
            state = ServiceState(id=1)
            session.add(state)
        state.initial_backfill_done_at = datetime.now(timezone.utc)
        session.commit()


async def run_initial_backfill(client: TelegramClient) -> None:
    """Backfill the last 30 days from every non-excluded dialog, once."""
    state = _load_state()
    if state.initial_backfill_done_at is not None:
        log.debug("initial_backfill_skipped_already_done")
        return

    _mark_started()
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKFILL_WINDOW_DAYS)
    log.info("initial_backfill_starting", cutoff=cutoff.isoformat())

    dialog_count = 0
    message_count = 0

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if _is_excluded_chat(entity):
            continue
        chat_id = dialog.id
        try:
            # Upsert chat so tag/notes flows and gap recovery can see it.
            Session = get_sessionmaker()
            with Session() as session:
                _upsert_chat(session, chat_id, entity)
                session.commit()

            added = await _backfill_chat(client, chat_id, cutoff)
            message_count += added
            dialog_count += 1
        except Exception:
            log.exception("initial_backfill_dialog_failed", chat_id=chat_id)

    _mark_done()
    log.info(
        "initial_backfill_complete",
        dialogs=dialog_count,
        messages=message_count,
    )
    await _notify_owner(dialog_count, message_count)


async def _backfill_chat(
    client: TelegramClient,
    chat_id: int,
    cutoff: datetime,
) -> int:
    """Page backwards until we cross `cutoff`; store messages along the way."""
    total = 0
    offset_id = 0  # 0 == start from the newest message
    while True:
        try:
            messages = await client.get_messages(
                chat_id,
                limit=100,
                offset_id=offset_id,
            )
        except FloodWaitError as e:
            log.warning(
                "initial_backfill_flood_wait",
                chat_id=chat_id,
                wait_seconds=e.seconds,
            )
            await asyncio.sleep(e.seconds)
            continue
        except Exception:
            log.exception("initial_backfill_fetch_error", chat_id=chat_id)
            return total

        if not messages:
            return total

        in_window = [m for m in messages if m.date and m.date >= cutoff]
        if in_window:
            await _store_messages(client, chat_id, list(in_window))
            total += len(in_window)

        oldest = messages[-1]
        if oldest.date is None or oldest.date < cutoff:
            return total
        if len(messages) < 100:
            return total

        offset_id = oldest.id
        await asyncio.sleep(_PAGE_SLEEP_SECONDS)


async def _notify_owner(dialog_count: int, message_count: int) -> None:
    """Send a Telegram DM via the bot token announcing backfill completion."""
    token = settings.tg_bot_token.get_secret_value() if settings.tg_bot_token else None
    owner_id = settings.tg_owner_user_id
    if not token or not owner_id:
        log.warning("initial_backfill_notify_skipped_no_bot_creds")
        return

    text = (
        f"Initial 30-day ingestion complete.\n"
        f"Dialogs: {dialog_count} · Messages: {message_count}\n\n"
        f"Send /tag to start tagging."
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            await http.post(url, json={"chat_id": owner_id, "text": text})
    except Exception:
        log.exception("initial_backfill_notify_failed")
