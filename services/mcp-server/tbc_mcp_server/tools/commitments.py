"""Commitment tools: read (get_commitments) and write (resolve / cancel / update)."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session
from tbc_common.db.models import Commitment

from ..models import CommitmentResult

log = structlog.get_logger(__name__)


def _to_result(row: Commitment) -> CommitmentResult:
    return CommitmentResult(
        id=row.id,
        chat_id=row.chat_id,
        source_message_id=row.source_message_id,
        owner=row.owner,
        description=row.description,
        due_at=row.due_at,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
        resolved_by_message_id=row.resolved_by_message_id,
        status=row.status,
    )


def get_commitments(
    db: Session,
    status: str | None = None,
    owner: str | None = None,
    chat_id: int | None = None,
    overdue_only: bool = False,
    query: str | None = None,
    limit: int = 50,
) -> list[CommitmentResult]:
    """Query commitments table with optional filters.

    `query` does case-insensitive substring matching on description so the LLM
    can find the right commitment from natural-language hints like "report" or
    "67.05". Without it, the user has hundreds of open commitments and the
    model would have to load all of them into context.
    """
    stmt = select(Commitment)
    filters = []

    if status:
        filters.append(Commitment.status == status)
    if owner:
        filters.append(Commitment.owner == owner)
    if chat_id is not None:
        filters.append(Commitment.chat_id == chat_id)
    if overdue_only:
        filters.append(Commitment.due_at < func.now())
        filters.append(Commitment.status == "open")
    if query:
        filters.append(Commitment.description.ilike(f"%{query}%"))

    if filters:
        stmt = stmt.where(and_(*filters))

    stmt = stmt.order_by(
        Commitment.due_at.desc().nulls_last(), Commitment.created_at.desc()
    ).limit(limit)

    rows = db.execute(stmt).scalars().all()
    return [_to_result(row) for row in rows]


# ---------------------------------------------------------------------------
# Write tools — only callable via MCP, never silently auto-applied
# ---------------------------------------------------------------------------


class CommitmentNotFound(Exception):
    """Raised when a write tool targets a commitment_id that doesn't exist."""


def _annotate(description: str, marker: str, note: str | None) -> str:
    """Append a one-line audit annotation to the description.

    Format: original\\n[<marker> YYYY-MM-DD: note]
    Idempotent only in the sense that repeated calls keep stacking lines —
    the LLM should rarely double-resolve, and an honest history is more
    useful than silent overwrites.
    """
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    suffix = f"[{marker} {today}"
    if note:
        suffix += f": {note}"
    suffix += "]"
    return f"{description}\n{suffix}"


def resolve_commitment(
    db: Session,
    commitment_id: int,
    note: str | None = None,
    resolved_by_message_id: int | None = None,
) -> CommitmentResult:
    """Mark a commitment as done. Sets status='done' and resolved_at=now().

    Optionally records the user-facing note (e.g. "sent the report today") and
    the Telegram message id that triggered the resolution, so we can audit
    where the close came from later.
    """
    row = db.get(Commitment, commitment_id)
    if row is None:
        raise CommitmentNotFound(f"commitment {commitment_id} not found")

    row.status = "done"
    row.resolved_at = datetime.now(UTC)
    if resolved_by_message_id is not None:
        row.resolved_by_message_id = resolved_by_message_id
    row.description = _annotate(row.description, "resolved", note)

    db.commit()
    db.refresh(row)
    log.info(
        "commitment_resolved",
        commitment_id=commitment_id,
        note=note,
        resolved_by_message_id=resolved_by_message_id,
    )
    return _to_result(row)


def cancel_commitment(
    db: Session,
    commitment_id: int,
    reason: str | None = None,
) -> CommitmentResult:
    """Mark a commitment as no-longer-relevant. Sets status='cancelled'."""
    row = db.get(Commitment, commitment_id)
    if row is None:
        raise CommitmentNotFound(f"commitment {commitment_id} not found")

    row.status = "cancelled"
    row.resolved_at = datetime.now(UTC)
    row.description = _annotate(row.description, "cancelled", reason)

    db.commit()
    db.refresh(row)
    log.info("commitment_cancelled", commitment_id=commitment_id, reason=reason)
    return _to_result(row)


def update_commitment(
    db: Session,
    commitment_id: int,
    due_at: datetime | None = None,
    note_append: str | None = None,
) -> CommitmentResult:
    """Adjust an open commitment without resolving it.

    Use cases:
    - Set / push the due date ("push to next Friday")
    - Append a status note ("waiting on Bob's reply") without closing it

    Either `due_at` or `note_append` must be provided.
    """
    if due_at is None and note_append is None:
        raise ValueError("update_commitment requires due_at or note_append")

    row = db.get(Commitment, commitment_id)
    if row is None:
        raise CommitmentNotFound(f"commitment {commitment_id} not found")

    if due_at is not None:
        row.due_at = due_at
    if note_append is not None:
        row.description = _annotate(row.description, "note", note_append)

    db.commit()
    db.refresh(row)
    log.info(
        "commitment_updated",
        commitment_id=commitment_id,
        due_at=due_at.isoformat() if due_at else None,
        note=note_append,
    )
    return _to_result(row)
