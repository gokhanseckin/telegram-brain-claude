"""get_recent_brief tool."""

from __future__ import annotations

from datetime import date

import structlog
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from tbc_common.db.models import ChatSummary

from ..models import BriefText

log = structlog.get_logger(__name__)

# Sentinel chat_id used to store Morning Brief content
BRIEF_SENTINEL_CHAT_ID = 0
BRIEF_PERIOD = "brief"


def get_recent_brief(
    db: Session,
    date_filter: date | None = None,
) -> BriefText:
    """Return the Morning Brief content from chat_summaries.

    The brief worker stores brief text with chat_id=0 and period='brief'.
    """
    stmt = (
        select(ChatSummary)
        .where(
            and_(
                ChatSummary.chat_id == BRIEF_SENTINEL_CHAT_ID,
                ChatSummary.period == BRIEF_PERIOD,
            )
        )
    )

    if date_filter is not None:
        stmt = stmt.where(ChatSummary.period_start == date_filter)
    else:
        stmt = stmt.order_by(desc(ChatSummary.period_start))

    row = db.execute(stmt.limit(1)).scalars().first()

    if row is None:
        return BriefText(date=date_filter, content="No brief available.", generated_at=None)

    return BriefText(
        date=row.period_start,
        content=row.summary,
        generated_at=row.generated_at,
    )
