"""Initial 6-month backfill on first deploy.

Enumerates all Telegram dialogs, excludes broadcast channels and public
supergroups, and — for each dialog whose latest message is within the last
6 months — pages back through messages up to a 6-month cutoff OR 500
messages per chat, whichever comes first. Stale chats (no activity in 6
months) are skipped entirely (no `chats` row created; the live handler
will create one on the next incoming message).

Runs at most once per install — guarded by the
`service_state.initial_backfill_done_at` column.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from tbc_common.config import settings
from tbc_common.db.models import ServiceState
from tbc_common.db.session import get_sessionmaker
from telethon import TelegramClient
from telethon.errors import FloodWaitError

from .gap_recovery import _PAGE_SLEEP_SECONDS, _store_messages
from .handlers import _is_excluded_chat, _upsert_chat

log = structlog.get_logger(__name__)

BACKFILL_WINDOW_DAYS = 180  # 6 months
PER_CHAT_MESSAGE_CAP = 500  # hard cap per chat during initial onboarding
_PAGE_SIZE = 100


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
        state.initial_backfill_started_at = datetime.now(UTC)
        session.commit()


def _mark_done() -> None:
    Session = get_sessionmaker()
    with Session() as session:
        state = session.get(ServiceState, 1)
        if state is None:
            state = ServiceState(id=1)
            session.add(state)
        state.initial_backfill_done_at = datetime.now(UTC)
        session.commit()


async def run_initial_backfill(client: TelegramClient) -> None:
    """Backfill recent history from every non-excluded, non-stale dialog, once."""
    state = _load_state()
    if state.initial_backfill_done_at is not None:
        log.debug("initial_backfill_skipped_already_done")
        return

    _mark_started()
    cutoff = datetime.now(UTC) - timedelta(days=BACKFILL_WINDOW_DAYS)
    log.info(
        "initial_backfill_starting",
        cutoff=cutoff.isoformat(),
        per_chat_cap=PER_CHAT_MESSAGE_CAP,
    )

    dialog_count = 0
    skipped_stale = 0
    skipped_excluded = 0
    message_count = 0

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if _is_excluded_chat(entity):
            skipped_excluded += 1
            continue

        chat_id = dialog.id
        latest = dialog.date  # Telethon: latest message timestamp for the dialog
        if latest is None or latest < cutoff:
            skipped_stale += 1
            log.debug(
                "initial_backfill_skipped_stale",
                chat_id=chat_id,
                latest=latest.isoformat() if latest else None,
            )
            continue

        try:
            # Dialog is active — create the chat row so /tag can see it, then backfill.
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
        skipped_stale=skipped_stale,
        skipped_excluded=skipped_excluded,
    )
    await _notify_owner(dialog_count, message_count, skipped_stale)


async def _backfill_chat(
    client: TelegramClient,
    chat_id: int,
    cutoff: datetime,
) -> int:
    """Page backwards until we cross cutoff or hit the per-chat 500 cap."""
    total = 0
    offset_id = 0  # 0 == start from the newest message
    while total < PER_CHAT_MESSAGE_CAP:
        remaining = PER_CHAT_MESSAGE_CAP - total
        page_limit = min(_PAGE_SIZE, remaining)
        try:
            messages = await client.get_messages(
                chat_id,
                limit=page_limit,
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
        # Respect the per-chat cap when storing.
        to_store = in_window[: PER_CHAT_MESSAGE_CAP - total]
        if to_store:
            await _store_messages(client, chat_id, list(to_store))
            total += len(to_store)

        oldest = messages[-1]
        if oldest.date is None or oldest.date < cutoff:
            return total
        if len(messages) < page_limit:
            return total

        offset_id = oldest.id
        await asyncio.sleep(_PAGE_SLEEP_SECONDS)

    log.info("initial_backfill_chat_cap_hit", chat_id=chat_id, cap=PER_CHAT_MESSAGE_CAP)
    return total


async def _notify_owner(
    dialog_count: int, message_count: int, skipped_stale: int
) -> None:
    """Send a Telegram DM via the bot token announcing backfill completion."""
    token = settings.tg_bot_token.get_secret_value() if settings.tg_bot_token else None
    owner_id = settings.tg_owner_user_id
    if not token or not owner_id:
        log.warning("initial_backfill_notify_skipped_no_bot_creds")
        return

    text = (
        f"Initial 6-month ingestion complete.\n"
        f"Dialogs: {dialog_count} · Messages: {message_count}"
        f" · Stale-skipped: {skipped_stale}\n\n"
        f"Send /tag to start tagging."
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            await http.post(url, json={"chat_id": owner_id, "text": text})
    except Exception:
        log.exception("initial_backfill_notify_failed")
