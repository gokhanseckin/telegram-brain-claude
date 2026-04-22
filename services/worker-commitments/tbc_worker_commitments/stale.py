"""Stale commitment detection job.

Marks commitments as 'stale' if status='open' and due_at < NOW() - 3 days.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tbc_common.db import Commitment

logger = structlog.get_logger(__name__)

STALE_GRACE_DAYS = 3


def mark_stale_commitments(session: Session) -> int:
    """Find overdue open commitments and mark them stale.

    Returns the number of commitments updated.
    """
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(days=STALE_GRACE_DAYS)

    overdue = list(
        session.scalars(
            select(Commitment).where(
                Commitment.status == "open",
                Commitment.due_at < threshold,
                Commitment.due_at != None,  # noqa: E711
            )
        ).all()
    )

    if not overdue:
        logger.debug("no_stale_commitments")
        return 0

    for commitment in overdue:
        commitment.status = "stale"
        logger.info(
            "commitment_marked_stale",
            commitment_id=commitment.id,
            due_at=commitment.due_at.isoformat() if commitment.due_at else None,
        )

    session.commit()
    logger.info("stale_detection_complete", count=len(overdue))
    return len(overdue)
