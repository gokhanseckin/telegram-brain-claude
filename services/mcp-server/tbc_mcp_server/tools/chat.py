"""get_chat_history, list_chats, get_chat_summary tools."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import structlog
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session
from tbc_common.db.models import Chat, ChatSummary, Message, MessageUnderstanding, User

from ..models import ChatListItem, ChatSummaryResult, MessageResult
from .search import _row_to_message_result

log = structlog.get_logger(__name__)


def get_chat_history(
    db: Session,
    chat_id: int,
    before: datetime | None = None,
    limit: int = 50,
) -> list[MessageResult]:
    """Paginated messages for one chat, ordered sent_at DESC."""
    stmt = (
        select(Message, Chat, User, MessageUnderstanding)
        .join(Chat, Chat.chat_id == Message.chat_id)
        .outerjoin(User, User.user_id == Message.sender_id)
        .outerjoin(
            MessageUnderstanding,
            and_(
                MessageUnderstanding.chat_id == Message.chat_id,
                MessageUnderstanding.message_id == Message.message_id,
            ),
        )
        .where(
            and_(
                Message.chat_id == chat_id,
                Message.deleted_at.is_(None),
            )
        )
    )

    if before is not None:
        stmt = stmt.where(Message.sent_at < before)

    stmt = stmt.order_by(desc(Message.sent_at)).limit(limit)
    rows = db.execute(stmt).all()
    return [_row_to_message_result(r[0], r[1], r[2], r[3]) for r in rows]


def list_chats(
    db: Session,
    tag: str | None = None,
    include_untagged: bool = False,
) -> list[ChatListItem]:
    """All chats with tag, last activity, participant_count."""
    # Get max sent_at per chat as last_activity
    from sqlalchemy import func

    last_msg_subq = (
        select(Message.chat_id, func.max(Message.sent_at).label("last_activity"))
        .where(Message.deleted_at.is_(None))
        .group_by(Message.chat_id)
        .subquery()
    )

    stmt = (
        select(Chat, last_msg_subq.c.last_activity)
        .outerjoin(last_msg_subq, last_msg_subq.c.chat_id == Chat.chat_id)
    )

    filters = []
    if tag is not None:
        filters.append(Chat.tag == tag)
    elif not include_untagged:
        filters.append(Chat.tag.isnot(None))
        filters.append(Chat.tag != "ignore")

    if filters:
        stmt = stmt.where(and_(*filters))

    stmt = stmt.order_by(desc(last_msg_subq.c.last_activity))
    rows = db.execute(stmt).all()

    return [
        ChatListItem(
            chat_id=row[0].chat_id,
            title=row[0].title,
            tag=row[0].tag,
            participant_count=row[0].participant_count,
            last_activity=row[1],
        )
        for row in rows
    ]


def get_chat_summary(
    db: Session,
    chat_id: int,
    period: str = "week",
    periods_back: int = 1,
) -> list[ChatSummaryResult]:
    """Fetch pre-computed summaries from chat_summaries table."""
    today = date.today()

    # Determine period_start based on period and periods_back
    if period == "day":
        period_start = today - timedelta(days=periods_back)
    elif period == "week":
        # Start of the week (Monday) going back periods_back weeks
        days_since_monday = today.weekday()
        current_week_start = today - timedelta(days=days_since_monday)
        period_start = current_week_start - timedelta(weeks=periods_back - 1)
    else:
        # Generic: just use the date subtraction
        period_start = today - timedelta(days=periods_back)

    stmt = (
        select(ChatSummary)
        .where(
            and_(
                ChatSummary.chat_id == chat_id,
                ChatSummary.period == period,
                ChatSummary.period_start >= period_start,
            )
        )
        .order_by(desc(ChatSummary.period_start))
        .limit(periods_back)
    )

    rows = db.execute(stmt).scalars().all()

    return [
        ChatSummaryResult(
            id=row.id,
            chat_id=row.chat_id,
            period=row.period,
            period_start=row.period_start,
            summary=row.summary,
            key_points=row.key_points,
            generated_at=row.generated_at,
        )
        for row in rows
    ]
