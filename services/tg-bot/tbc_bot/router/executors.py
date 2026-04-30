"""Executors — turn a RouterDecision into a side effect + user-facing reply.

Two paths today:
  exec_feedback                       — writes a brief_feedback row
  exec_commitment_resolve / _cancel   — writes commitment status='done' / 'cancelled'

Both call shared SQLAlchemy functions in tbc_common so the row shape
is identical to what the MCP-tool / Claude path produces. The audit
annotation (`[resolved YYYY-MM-DD: <note>]`) is appended by the same
helper, so a downstream `get_commitments` query can't tell which path
closed a row.

Executors run synchronously inside `asyncio.to_thread` because the
bot is async but SQLAlchemy sessions are sync. The DB writes are
short, no need for an async driver.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import date as date_cls

import structlog
from sqlalchemy import select
from tbc_common.db.commitments import (
    CommitmentNotFound,
)
from tbc_common.db.commitments import (
    cancel_commitment as _cancel_commitment_db,
)
from tbc_common.db.commitments import (
    resolve_commitment as _resolve_commitment_db,
)
from tbc_common.db.models import BriefFeedback, Chat
from tbc_common.db.session import get_sessionmaker

from .decision import RouterDecision

log = structlog.get_logger(__name__)


class CommitmentLookupFailed(Exception):
    """Bubbles up to the chat handler so it can answer the user gracefully
    instead of escalating to Claude."""


def _write_feedback_sync(
    feedback_type: str,
    item_ref: str | None,
    note: str | None,
) -> int:
    """Insert a brief_feedback row. Returns the new row id.

    Mirrors the column population in
    `tbc_mcp_server.tools.feedback.write_brief_feedback` and the legacy
    `/feedback` slash handler — same table, same shape, so brief
    calibration treats all three paths uniformly.
    """
    sm = get_sessionmaker()
    with sm() as session:
        row = BriefFeedback(
            brief_date=date_cls.today(),
            item_ref=item_ref,
            feedback=feedback_type,
            note=note,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return int(row.id)


async def exec_feedback(decision: RouterDecision) -> str:
    """Persist a feedback row and return the user-facing confirmation text.

    Expects `decision.fields` to contain `feedback_type`, `item_ref`,
    `note`. Validation of `feedback_type` happened upstream (in the rules
    vocab set or, in PR2, the LLM schema check) so we trust it here.
    """
    feedback_type = decision.fields["feedback_type"]
    item_ref = decision.fields.get("item_ref")
    note = decision.fields.get("note")

    row_id = await asyncio.to_thread(
        _write_feedback_sync, feedback_type, item_ref, note
    )

    log.info(
        "router_feedback_written",
        id=row_id,
        item_ref=item_ref,
        feedback=feedback_type,
        source=decision.source,
    )

    if item_ref:
        return f"Recorded: {feedback_type} on #{item_ref} (id={row_id})."
    return f"Recorded missed: {note or '(no note)'} (id={row_id})."


# ---------------------------------------------------------------------------
# Commitment shortcut executors (rule path: `done c<id>` / `cancel c<id>`)
# ---------------------------------------------------------------------------


def _resolve_commitment_sync(
    commitment_id: int, note: str | None, source_message_id: int | None
) -> tuple[int, str]:
    sm = get_sessionmaker()
    with sm() as session:
        row = _resolve_commitment_db(
            session,
            commitment_id=commitment_id,
            note=note,
            resolved_by_message_id=source_message_id,
        )
        return int(row.id), row.description.splitlines()[0]


def _cancel_commitment_sync(
    commitment_id: int, reason: str | None
) -> tuple[int, str]:
    sm = get_sessionmaker()
    with sm() as session:
        row = _cancel_commitment_db(
            session, commitment_id=commitment_id, reason=reason
        )
        return int(row.id), row.description.splitlines()[0]


async def exec_commitment_resolve(
    decision: RouterDecision, source_message_id: int | None = None
) -> str:
    cid = decision.fields["commitment_id"]
    note = decision.fields.get("note")
    try:
        row_id, description = await asyncio.to_thread(
            _resolve_commitment_sync, cid, note, source_message_id
        )
    except CommitmentNotFound:
        raise CommitmentLookupFailed(f"No commitment c{cid} found.") from None
    log.info(
        "router_commitment_resolved",
        commitment_id=row_id,
        note=note,
        source=decision.source,
    )
    return f"Marked done: c{row_id} — {description}"


async def exec_commitment_cancel(decision: RouterDecision) -> str:
    cid = decision.fields["commitment_id"]
    reason = decision.fields.get("reason")
    try:
        row_id, description = await asyncio.to_thread(
            _cancel_commitment_sync, cid, reason
        )
    except CommitmentNotFound:
        raise CommitmentLookupFailed(f"No commitment c{cid} found.") from None
    log.info(
        "router_commitment_cancelled",
        commitment_id=row_id,
        reason=reason,
        source=decision.source,
    )
    return f"Cancelled: c{row_id} — {description}"


# ---------------------------------------------------------------------------
# Retag executor (DM-driven chat role-tag correction)
# ---------------------------------------------------------------------------


@dataclass
class RetagSearchOutcome:
    kind: str  # "zero", "one", "many"
    chat_id: int | None = None
    title: str | None = None
    new_tag: str = ""
    candidates: list[tuple[int, str]] = field(default_factory=list)


def _search_chat_sync(target: str) -> tuple[int, list[tuple[int, str]]]:
    sm = get_sessionmaker()
    with sm() as session:
        rows = list(
            session.scalars(
                select(Chat).where(Chat.title.ilike(f"%{target}%"))
            ).all()
        )
    return len(rows), [(r.chat_id, r.title or f"chat_{r.chat_id}") for r in rows]


def _apply_retag_sync(chat_id: int, new_tag: str) -> str:
    sm = get_sessionmaker()
    with sm() as session:
        chat = session.get(Chat, chat_id)
        if chat is None:
            raise CommitmentLookupFailed(
                f"chat {chat_id} vanished between search and update"
            )
        chat.tag = new_tag
        chat.tag_set_at = datetime.now(UTC)
        chat.tag_source = "manual"
        chat.tag_locked = True
        chat.tag_confidence = None
        chat.tag_reason = None
        session.commit()
        return chat.title or f"chat_{chat_id}"


async def exec_retag_search(decision: RouterDecision) -> RetagSearchOutcome:
    target = decision.fields["target"]
    new_tag = decision.fields["new_tag"]
    count, pairs = await asyncio.to_thread(_search_chat_sync, target)

    log.info(
        "router_retag_search",
        target=target,
        new_tag=new_tag,
        match_count=count,
        source=decision.source,
    )

    if count == 0:
        return RetagSearchOutcome(kind="zero", new_tag=new_tag)
    if count == 1:
        cid, title = pairs[0]
        return RetagSearchOutcome(
            kind="one", chat_id=cid, title=title, new_tag=new_tag
        )
    return RetagSearchOutcome(
        kind="many", new_tag=new_tag, candidates=pairs
    )


async def exec_retag_apply(chat_id: int, new_tag: str) -> str:
    title = await asyncio.to_thread(_apply_retag_sync, chat_id, new_tag)
    log.info("router_retag_applied", chat_id=chat_id, new_tag=new_tag)
    return f"Retagged '{title}' (chat {chat_id}) as {new_tag}."
