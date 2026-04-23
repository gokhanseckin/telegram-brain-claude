"""Relationship state recomputation job.

For each tagged chat, recomputes relationship_state based on recent
message_understanding rows and upserts into the relationship_state table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session
from tbc_common.db import Chat, Message, MessageUnderstanding, RelationshipState

logger = structlog.get_logger(__name__)

DORMANT_DAYS = 30
ACTIVE_DAYS = 7
ACTIVE_VOLUME_THRESHOLD = 3
OPEN_THREADS_LOOKBACK_DAYS = 14
MAX_OPEN_THREADS = 5


def _utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (UTC). Handles SQLite naive datetimes."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def recompute_relationship_states(session: Session) -> int:
    """Recompute relationship_state for all tagged (non-ignored) chats.

    Returns the number of chats updated.
    """
    tagged_chats = list(
        session.scalars(
            select(Chat).where(
                Chat.tag != None,  # noqa: E711
                Chat.tag != "ignore",
            )
        ).all()
    )

    if not tagged_chats:
        logger.debug("no_tagged_chats")
        return 0

    updated = 0
    for chat in tagged_chats:
        try:
            _recompute_one(session, chat.chat_id)
            updated += 1
        except Exception:
            logger.exception("recompute_error", chat_id=chat.chat_id)

    session.commit()
    return updated


def _recompute_one(session: Session, chat_id: int) -> None:
    now = datetime.now(UTC)

    all_mu = list(
        session.scalars(
            select(MessageUnderstanding).where(
                MessageUnderstanding.chat_id == chat_id,
            )
        ).all()
    )

    # Use sent_at (not processed_at) so backfill runs don't make all history
    # look like it happened today.
    sent_at_map: dict[int, datetime] = {}
    if all_mu:
        for msg_id, sent_at in session.execute(
            select(Message.message_id, Message.sent_at).where(
                Message.chat_id == chat_id,
                Message.message_id.in_([r.message_id for r in all_mu]),
            )
        ).all():
            sent_at_map[msg_id] = sent_at

    def _contact_time(r: MessageUnderstanding) -> datetime:
        return _utc(sent_at_map.get(r.message_id, r.processed_at))

    # Filter by date in Python to avoid timezone issues with SQLite
    rows_30d = [r for r in all_mu if _contact_time(r) >= now - timedelta(days=30)]
    rows_7d = [r for r in rows_30d if _contact_time(r) >= now - timedelta(days=7)]
    rows_14d = [r for r in rows_30d if _contact_time(r) >= now - timedelta(days=14)]

    # Also check rows older than 30 days for dormant logic
    all_contact_times = [_contact_time(r) for r in all_mu]
    last_contact: datetime | None = max(all_contact_times, default=None)

    # --- Stage inference ---
    stage: str
    if last_contact is None or last_contact < now - timedelta(days=DORMANT_DAYS):
        stage = "dormant"
    elif len(rows_7d) > ACTIVE_VOLUME_THRESHOLD and last_contact >= now - timedelta(days=ACTIVE_DAYS):
        stage = "active"
    else:
        signal_types_30d = {r.signal_type for r in rows_30d if r.signal_type}
        if "buying" in signal_types_30d:
            stage = "proposal"
        elif signal_types_30d & {"timeline", "decision_maker"}:
            stage = "negotiation"
        elif "expansion" in signal_types_30d:
            stage = "active"
        elif signal_types_30d:
            stage = "qualifying"
        else:
            stage = "qualifying"

    # --- Temperature ---
    deltas_7d = [r.sentiment_delta for r in rows_7d if r.sentiment_delta is not None]
    if deltas_7d:
        avg_delta = sum(deltas_7d) / len(deltas_7d)
        if avg_delta > 0:
            temperature = "warming"
        elif avg_delta < 0:
            temperature = "cooling"
        else:
            temperature = "stable"
    else:
        temperature = "stable"

    # --- Open threads ---
    directed_rows = [r for r in rows_14d if r.is_directed_at_user and r.summary_en]
    seen: set[str] = set()
    open_threads: list[dict[str, Any]] = []
    for r in sorted(directed_rows, key=_contact_time, reverse=True):
        if r.summary_en not in seen:
            seen.add(r.summary_en)
            open_threads.append(
                {"topic": r.summary_en, "last_mentioned_at": _contact_time(r).isoformat()}
            )
        if len(open_threads) >= MAX_OPEN_THREADS:
            break

    # --- Upsert ---
    existing: RelationshipState | None = session.get(RelationshipState, chat_id)
    if existing is None:
        rs = RelationshipState(
            chat_id=chat_id,
            stage=stage,
            stage_confidence=3,
            last_meaningful_contact_at=last_contact,
            temperature=temperature,
            open_threads=open_threads,
            updated_at=now,
        )
        session.add(rs)
    else:
        existing.stage = stage
        existing.stage_confidence = 3
        existing.last_meaningful_contact_at = last_contact
        existing.temperature = temperature
        existing.open_threads = open_threads
        existing.updated_at = now

    logger.info("relationship_state_updated", chat_id=chat_id, stage=stage, temperature=temperature)
