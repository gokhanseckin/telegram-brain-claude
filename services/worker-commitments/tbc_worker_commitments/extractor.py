"""Commitment extraction job.

Polls message_understanding for rows where is_commitment=True and no
matching row exists in commitments (matched by source_message_id). Creates
commitments rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tbc_common.db import Commitment, MessageUnderstanding

logger = structlog.get_logger(__name__)


def extract_commitments(session: Session) -> int:
    """Find unprocessed commitment rows and create Commitment records.

    Returns the number of new commitments created.
    """
    # Find all MU rows where is_commitment=True
    stmt = select(MessageUnderstanding).where(
        MessageUnderstanding.is_commitment == True,  # noqa: E712
    )
    candidates = list(session.scalars(stmt).all())

    if not candidates:
        logger.debug("no_commitment_candidates")
        return 0

    # Get existing source_message_ids to avoid duplicates
    existing_stmt = select(Commitment.source_message_id).where(
        Commitment.source_message_id != None,  # noqa: E711
    )
    existing_message_ids: set[int] = set(session.scalars(existing_stmt).all())

    created = 0
    for mu in candidates:
        if mu.message_id in existing_message_ids:
            continue

        commitment_data: dict[str, Any] = mu.commitment or {}
        owner = commitment_data.get("who", "counterparty")
        description = commitment_data.get("what") or mu.summary_en or "(no description)"

        due_at: datetime | None = None
        due_str = commitment_data.get("due")
        if due_str:
            try:
                from datetime import timezone
                due_at = datetime.fromisoformat(due_str).replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                logger.warning("invalid_due_date", due_str=due_str, message_id=mu.message_id)

        commitment = Commitment(
            chat_id=mu.chat_id,
            source_message_id=mu.message_id,
            owner=owner,
            description=description,
            due_at=due_at,
            status="open",
        )
        session.add(commitment)
        existing_message_ids.add(mu.message_id)
        created += 1
        logger.info(
            "commitment_created",
            chat_id=mu.chat_id,
            message_id=mu.message_id,
            owner=owner,
        )

    if created:
        session.commit()

    return created
