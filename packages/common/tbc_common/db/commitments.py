"""Commitment write operations — shared by mcp-server (Claude path) and
tbc-bot (router/slash local-first path).

The mcp-server tool exposes these to Claude via MCP; the bot calls the
same functions directly via SQLAlchemy when the router rule path
matches a `done c<id>` / `cancel c<id>` shortcut. Both paths must
produce identical row shapes (status, resolved_at, audit annotation)
so the audit trail is uniform.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.orm import Session

from .models import Commitment

log = structlog.get_logger(__name__)


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
) -> Commitment:
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
    return row


def cancel_commitment(
    db: Session,
    commitment_id: int,
    reason: str | None = None,
) -> Commitment:
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
    return row


def update_commitment(
    db: Session,
    commitment_id: int,
    due_at: datetime | None = None,
    note_append: str | None = None,
) -> Commitment:
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
    return row
