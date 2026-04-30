"""Commitment tools: read (get_commitments) and write delegations to common."""

from __future__ import annotations

from datetime import datetime

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session
from tbc_common.db.commitments import (
    CommitmentNotFound,
)
from tbc_common.db.commitments import (
    cancel_commitment as _cancel,
)
from tbc_common.db.commitments import (
    resolve_commitment as _resolve,
)
from tbc_common.db.commitments import (
    update_commitment as _update,
)
from tbc_common.db.models import Commitment

from ..models import CommitmentResult

# Re-export so existing imports from this module keep working.
__all__ = [
    "CommitmentNotFound",
    "cancel_commitment",
    "get_commitments",
    "resolve_commitment",
    "update_commitment",
]

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
# Write tools — thin wrappers around the shared tbc_common.db.commitments
# functions that translate the returned ORM row into a CommitmentResult.
# ---------------------------------------------------------------------------


def resolve_commitment(
    db: Session,
    commitment_id: int,
    note: str | None = None,
    resolved_by_message_id: int | None = None,
) -> CommitmentResult:
    return _to_result(_resolve(db, commitment_id, note, resolved_by_message_id))


def cancel_commitment(
    db: Session,
    commitment_id: int,
    reason: str | None = None,
) -> CommitmentResult:
    return _to_result(_cancel(db, commitment_id, reason))


def update_commitment(
    db: Session,
    commitment_id: int,
    due_at: datetime | None = None,
    note_append: str | None = None,
) -> CommitmentResult:
    return _to_result(_update(db, commitment_id, due_at, note_append))
