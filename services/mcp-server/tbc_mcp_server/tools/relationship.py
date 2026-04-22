"""get_relationship_state tool."""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tbc_common.db.models import Chat, RelationshipState

from ..models import RelationshipStateResult

log = structlog.get_logger(__name__)


def get_relationship_state(
    db: Session,
    chat_id: int | None = None,
) -> list[RelationshipStateResult]:
    """Return relationship state for one or all chats."""
    stmt = (
        select(RelationshipState, Chat)
        .outerjoin(Chat, Chat.chat_id == RelationshipState.chat_id)
    )

    if chat_id is not None:
        stmt = stmt.where(RelationshipState.chat_id == chat_id)

    rows = db.execute(stmt).all()

    return [
        RelationshipStateResult(
            chat_id=r[0].chat_id,
            stage=r[0].stage,
            stage_confidence=r[0].stage_confidence,
            last_meaningful_contact_at=r[0].last_meaningful_contact_at,
            last_user_message_at=r[0].last_user_message_at,
            last_counterparty_message_at=r[0].last_counterparty_message_at,
            temperature=r[0].temperature,
            open_threads=r[0].open_threads,
            user_override=r[0].user_override,
            updated_at=r[0].updated_at,
            chat_title=r[1].title if r[1] else None,
            chat_tag=r[1].tag if r[1] else None,
        )
        for r in rows
    ]
