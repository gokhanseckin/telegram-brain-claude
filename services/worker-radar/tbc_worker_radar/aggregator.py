"""Signal → RadarAlert aggregation logic.

Scans message_understanding rows where is_signal=True and created_at
is after last_checked_at, groups them by (chat_id, signal_type), and
either creates new radar_alerts or appends to existing open ones.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session
from tbc_common.db import MessageUnderstanding, RadarAlert
from tbc_common.db.models import Message

# Skip emitting alerts whose newest supporting message is older than this.
# Backfilled understandings of months-old conversations would otherwise mint
# fresh-looking alerts — see 2026-04-29 Mieszko incident.
STALE_SIGNAL_CUTOFF = timedelta(days=7)

logger = structlog.get_logger(__name__)


def _alert_tag(alert_id: int) -> str:
    """Return a 4-char hex tag derived from the alert ID, e.g. '#a7f2'."""
    digest = hashlib.sha256(str(alert_id).encode()).hexdigest()
    return f"#{digest[:4]}"


def _build_reasoning(tag: str, signals: list[MessageUnderstanding]) -> str:
    """Build a reasoning string citing the message summaries."""
    summaries = [
        f"(chat={s.chat_id}, msg={s.message_id}): {s.summary_en or '(no summary)'}"
        for s in signals
    ]
    body = "; ".join(summaries)
    return f"{tag} — {body}"


def _utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware UTC. Handles SQLite naive datetimes."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def run_aggregation(session: Session, last_checked_at: datetime) -> datetime:
    """Scan new signals since last_checked_at and upsert radar_alerts.

    Returns the new last_checked_at value (should be stored by the caller).
    """
    now = datetime.now(UTC)

    # 1. Fetch all new signals since last_checked_at
    stmt = select(MessageUnderstanding).where(
        MessageUnderstanding.is_signal == True,  # noqa: E712
        MessageUnderstanding.processed_at > last_checked_at,
    )
    new_signals: list[MessageUnderstanding] = list(session.scalars(stmt).all())

    if not new_signals:
        logger.debug("no_new_signals", since=last_checked_at.isoformat())
        return now

    logger.info("found_new_signals", count=len(new_signals))

    # 2. Group by (chat_id, signal_type)
    groups: dict[tuple[int, str], list[MessageUnderstanding]] = {}
    for sig in new_signals:
        if not sig.signal_type:
            continue
        key = (sig.chat_id, sig.signal_type)
        groups.setdefault(key, []).append(sig)

    cutoff_24h = now - timedelta(hours=24)
    stale_cutoff = now - STALE_SIGNAL_CUTOFF

    for (chat_id, signal_type), sigs in groups.items():
        severity = max(
            (s.signal_strength for s in sigs if s.signal_strength is not None),
            default=1,
        )
        new_msg_refs: list[dict[str, int]] = [
            {"chat_id": s.chat_id, "message_id": s.message_id} for s in sigs
        ]

        # Compute the freshness anchor: latest send time across this group's
        # source messages. Skip the whole group if even the newest is too old.
        sent_at_rows = session.execute(
            select(Message.sent_at).where(
                Message.chat_id == chat_id,
                Message.message_id.in_([s.message_id for s in sigs]),
            )
        ).all()
        sent_ats = [_utc(r[0]) for r in sent_at_rows if r[0] is not None]
        latest_sent = max(sent_ats) if sent_ats else None

        if latest_sent is None or latest_sent < stale_cutoff:
            logger.info(
                "alert_skipped_stale_signal",
                chat_id=chat_id,
                signal_type=signal_type,
                latest_sent=latest_sent.isoformat() if latest_sent else None,
                cutoff_days=STALE_SIGNAL_CUTOFF.days,
            )
            continue

        # 3. Check for existing open alert within 24h
        # Load all alerts for this chat+type and filter in Python to handle
        # timezone-naive datetimes from SQLite.
        candidates = list(session.scalars(
            select(RadarAlert).where(
                RadarAlert.chat_id == chat_id,
                RadarAlert.alert_type == signal_type,
            )
        ).all())

        existing: RadarAlert | None = None
        for candidate in candidates:
            if _utc(candidate.created_at) >= cutoff_24h:
                existing = candidate
                break

        if existing is not None:
            # Append to supporting_message_ids
            current_ids: list[dict[str, int]] = existing.supporting_message_ids or []
            merged = current_ids + new_msg_refs
            existing.supporting_message_ids = merged
            # Update severity if new signals are stronger
            if severity > (existing.severity or 0):
                existing.severity = severity
            # Refresh freshness anchor to the latest supporting send time.
            prev = _utc(existing.source_sent_at) if existing.source_sent_at else None
            if prev is None or latest_sent > prev:
                existing.source_sent_at = latest_sent
            # Update reasoning (keep same tag, refresh summary)
            tag = _alert_tag(existing.id)
            existing.reasoning = _build_reasoning(tag, sigs)
            session.flush()
            logger.info(
                "alert_updated",
                alert_id=existing.id,
                chat_id=chat_id,
                signal_type=signal_type,
                appended=len(new_msg_refs),
            )
        else:
            # 4. Create new alert (need to flush to get the id)
            alert = RadarAlert(
                chat_id=chat_id,
                alert_type=signal_type,
                severity=severity,
                title=_build_title(signal_type, chat_id, session),
                supporting_message_ids=new_msg_refs,
                source_sent_at=latest_sent,
                reasoning="",  # will be filled after flush gives us the id
            )
            session.add(alert)
            session.flush()  # populates alert.id

            tag = _alert_tag(alert.id)
            alert.reasoning = _build_reasoning(tag, sigs)
            session.flush()
            logger.info(
                "alert_created",
                alert_id=alert.id,
                chat_id=chat_id,
                signal_type=signal_type,
                severity=severity,
            )

    session.commit()
    return now


def _build_title(signal_type: str, chat_id: int, session: Session) -> str:
    """Build a human-readable alert title."""
    from tbc_common.db import Chat

    chat = session.get(Chat, chat_id)
    chat_title = (chat.title or f"chat {chat_id}") if chat else f"chat {chat_id}"
    label = signal_type.replace("_", " ").capitalize()
    return f"{label} signal in {chat_title}"
