"""Tests for relationship state recomputation.

The `session` fixture is provided by ../conftest.py (SQLite in-memory).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session


def _chat(session, chat_id=1, tag="client", title="Test Chat"):
    from tbc_common.db.models import Chat

    chat = Chat(chat_id=chat_id, type="private", title=title, tag=tag)
    session.add(chat)
    session.commit()
    return chat


def _mu(session, chat_id=1, message_id=1, processed_at=None, is_signal=False,
        signal_type=None, signal_strength=None, sentiment_delta=0,
        is_directed_at_user=False, summary_en="Test"):
    from tbc_common.db.models import MessageUnderstanding

    mu = MessageUnderstanding(
        chat_id=chat_id,
        message_id=message_id,
        model_version="test-v1",
        processed_at=processed_at or datetime.now(timezone.utc),
        is_signal=is_signal,
        signal_type=signal_type,
        signal_strength=signal_strength,
        sentiment_delta=sentiment_delta,
        is_directed_at_user=is_directed_at_user,
        summary_en=summary_en,
    )
    session.add(mu)
    session.commit()
    return mu


def test_dormant_stage_when_no_contact(session: Session):
    """Last contact 35 days ago → stage='dormant'."""
    from tbc_common.db.models import RelationshipState
    from tbc_worker_commitments.relationship import recompute_relationship_states

    _chat(session, chat_id=1)
    old_time = datetime.now(timezone.utc) - timedelta(days=35)
    _mu(session, chat_id=1, message_id=1, processed_at=old_time)

    recompute_relationship_states(session)

    rs = session.get(RelationshipState, 1)
    assert rs is not None
    assert rs.stage == "dormant"


def test_active_stage_recent_messages(session: Session):
    """5 messages in last 7 days → stage='active'."""
    from tbc_common.db.models import RelationshipState
    from tbc_worker_commitments.relationship import recompute_relationship_states

    _chat(session, chat_id=2)
    now = datetime.now(timezone.utc)
    for i in range(5):
        _mu(session, chat_id=2, message_id=i + 1,
            processed_at=now - timedelta(hours=i + 1))

    recompute_relationship_states(session)

    rs = session.get(RelationshipState, 2)
    assert rs is not None
    assert rs.stage == "active"


def test_temperature_cooling(session: Session):
    """Average sentiment_delta = -1 over 7 days → temperature='cooling'."""
    from tbc_common.db.models import RelationshipState
    from tbc_worker_commitments.relationship import recompute_relationship_states

    _chat(session, chat_id=3)
    now = datetime.now(timezone.utc)
    for i in range(3):
        _mu(session, chat_id=3, message_id=i + 1,
            processed_at=now - timedelta(hours=i + 1),
            sentiment_delta=-1)

    recompute_relationship_states(session)

    rs = session.get(RelationshipState, 3)
    assert rs is not None
    assert rs.temperature == "cooling"


def test_open_threads_collected(session: Session):
    """3 unanswered is_directed_at_user=True messages → open_threads has 3 entries."""
    from tbc_common.db.models import RelationshipState
    from tbc_worker_commitments.relationship import recompute_relationship_states

    _chat(session, chat_id=4)
    now = datetime.now(timezone.utc)
    for i in range(3):
        _mu(session, chat_id=4, message_id=i + 1,
            processed_at=now - timedelta(hours=i + 1),
            is_directed_at_user=True,
            summary_en=f"Unanswered question {i + 1}")

    recompute_relationship_states(session)

    rs = session.get(RelationshipState, 4)
    assert rs is not None
    assert rs.open_threads is not None
    assert len(rs.open_threads) == 3
