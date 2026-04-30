"""Commitment shortcut slash commands: /done c<id>, /cancel c<id>.

These let the user mark a commitment complete (or cancelled) by its
short-id from the brief — the same `(c<id>)` tag the worker-brief
renders next to each Open Commitment line.

Resolution path:
  /done c42 sent today
    → cmd_done parses commitment_id=42, note="sent today"
    → calls tbc_common.db.commitments.resolve_commitment(42, ...)
    → bot replies with the canonical confirmation

No Claude call. No MCP HTTP. Sub-second by design — that's the point.

Free-text equivalents (`done c42`, `cancel c42`) are caught by the
router rule path, see services/tg-bot/tbc_bot/router/rules.py.
"""

from __future__ import annotations

import asyncio
import re

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from tbc_common.db.commitments import (
    CommitmentNotFound,
    cancel_commitment,
    resolve_commitment,
)
from tbc_common.db.session import get_sessionmaker

from tbc_bot.guards import is_owner

log = structlog.get_logger(__name__)

router = Router(name="commitments")

# Slash payload: optional `c` (we accept both `/done 42` and `/done c42`),
# integer id, optional rest-of-text used as note/reason.
_PAYLOAD = re.compile(
    r"^c?(?P<id>\d+)(?:\s+(?P<rest>.+))?$",
    re.IGNORECASE,
)


def _parse(body: str) -> tuple[int, str | None] | None:
    m = _PAYLOAD.match(body.strip())
    if not m:
        return None
    rest = m.group("rest")
    note = rest.strip() if rest else None
    note = note or None
    return int(m.group("id")), note


def _resolve_sync(
    commitment_id: int, note: str | None, message_id: int
) -> tuple[int, str]:
    sm = get_sessionmaker()
    with sm() as session:
        row = resolve_commitment(
            session,
            commitment_id=commitment_id,
            note=note,
            resolved_by_message_id=message_id,
        )
        # Strip the audit-annotation suffix from the reply to keep it short.
        first_line = row.description.splitlines()[0]
        return int(row.id), first_line


def _cancel_sync(commitment_id: int, reason: str | None) -> tuple[int, str]:
    sm = get_sessionmaker()
    with sm() as session:
        row = cancel_commitment(
            session, commitment_id=commitment_id, reason=reason
        )
        first_line = row.description.splitlines()[0]
        return int(row.id), first_line


@router.message(Command("done"))
async def cmd_done(message: Message) -> None:
    if not is_owner(message):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Usage: /done c<id> [note]\n"
            "Example: /done c42 sent the report\n"
            "(<id> is the (c<id>) tag from the brief)"
        )
        return

    parsed = _parse(parts[1])
    if parsed is None:
        await message.answer(
            "Could not parse. Format: /done c<id> [note]\n"
            "Example: /done c42 sent today"
        )
        return

    commitment_id, note = parsed
    try:
        row_id, description = await asyncio.to_thread(
            _resolve_sync, commitment_id, note, message.message_id
        )
    except CommitmentNotFound:
        log.info("slash_done_not_found", commitment_id=commitment_id)
        await message.answer(f"No commitment c{commitment_id} found.")
        return
    except Exception:
        log.exception("cmd_done_error", commitment_id=commitment_id)
        await message.answer("Something went wrong, please try again.")
        return

    log.info(
        "slash_done", commitment_id=row_id, note=note, source_msg=message.message_id
    )
    await message.answer(f"Marked done: c{row_id} — {description}")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    if not is_owner(message):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Usage: /cancel c<id> [reason]\n"
            "Example: /cancel c42 no longer needed"
        )
        return

    parsed = _parse(parts[1])
    if parsed is None:
        await message.answer(
            "Could not parse. Format: /cancel c<id> [reason]"
        )
        return

    commitment_id, reason = parsed
    try:
        row_id, description = await asyncio.to_thread(
            _cancel_sync, commitment_id, reason
        )
    except CommitmentNotFound:
        log.info("slash_cancel_not_found", commitment_id=commitment_id)
        await message.answer(f"No commitment c{commitment_id} found.")
        return
    except Exception:
        log.exception("cmd_cancel_error", commitment_id=commitment_id)
        await message.answer("Something went wrong, please try again.")
        return

    log.info("slash_cancel", commitment_id=row_id, reason=reason)
    await message.answer(f"Cancelled: c{row_id} — {description}")
