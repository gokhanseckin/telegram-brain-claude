"""get_commitments tool."""

from __future__ import annotations

import structlog
from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import Session

from tbc_common.db.models import Commitment

from ..models import CommitmentResult

log = structlog.get_logger(__name__)


def get_commitments(
    db: Session,
    status: str | None = None,
    owner: str | None = None,
    chat_id: int | None = None,
    overdue_only: bool = False,
) -> list[CommitmentResult]:
    """Query commitments table with optional filters."""
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

    if filters:
        stmt = stmt.where(and_(*filters))

    stmt = stmt.order_by(desc(Commitment.due_at.nulls_last()), desc(Commitment.created_at))

    rows = db.execute(stmt).scalars().all()

    return [
        CommitmentResult(
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
        for row in rows
    ]
