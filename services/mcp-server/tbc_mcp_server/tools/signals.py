"""get_signals tool."""

from __future__ import annotations

from datetime import date, datetime

import structlog
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session
from tbc_common.db.models import Chat, Message, MessageUnderstanding

from ..models import SignalResult

log = structlog.get_logger(__name__)


def get_signals(
    db: Session,
    signal_types: list[str] | None = None,
    min_strength: int = 1,
    date_from: date | None = None,
    chat_ids: list[int] | None = None,
) -> list[SignalResult]:
    """Query message_understanding where is_signal=True."""
    stmt = (
        select(MessageUnderstanding, Message, Chat)
        .join(
            Message,
            and_(
                Message.chat_id == MessageUnderstanding.chat_id,
                Message.message_id == MessageUnderstanding.message_id,
            ),
        )
        .join(Chat, Chat.chat_id == MessageUnderstanding.chat_id)
        .where(
            and_(
                MessageUnderstanding.is_signal == True,  # noqa: E712
                MessageUnderstanding.signal_strength >= min_strength,
                Chat.tag.isnot(None),
                Chat.tag != "ignore",
                Message.deleted_at.is_(None),
            )
        )
    )

    if signal_types:
        stmt = stmt.where(MessageUnderstanding.signal_type.in_(signal_types))
    if date_from:
        stmt = stmt.where(
            Message.sent_at >= datetime(date_from.year, date_from.month, date_from.day)
        )
    if chat_ids:
        stmt = stmt.where(MessageUnderstanding.chat_id.in_(chat_ids))

    stmt = stmt.order_by(
        desc(MessageUnderstanding.signal_strength),
        desc(Message.sent_at),
    )

    rows = db.execute(stmt).all()

    return [
        SignalResult(
            chat_id=r[0].chat_id,
            message_id=r[0].message_id,
            signal_type=r[0].signal_type,
            signal_strength=r[0].signal_strength,
            summary_en=r[0].summary_en,
            processed_at=r[0].processed_at,
            sent_at=r[1].sent_at,
            chat_title=r[2].title,
            chat_tag=r[2].tag,
        )
        for r in rows
    ]
