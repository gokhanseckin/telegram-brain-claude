"""Tests for stale commitment detection.

The `session` fixture is provided by ../conftest.py (SQLite in-memory).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session


def _commitment(session, due_at, status="open"):
    from tbc_common.db.models import Commitment

    c = Commitment(
        chat_id=1,
        source_message_id=1,
        owner="user",
        description="Test commitment",
        due_at=due_at,
        status=status,
    )
    session.add(c)
    session.commit()
    return c


def test_overdue_commitment_marked_stale(session: Session):
    """due_at 5 days ago with status='open' → status becomes 'stale'."""
    from tbc_worker_commitments.stale import mark_stale_commitments

    due = datetime.now(UTC) - timedelta(days=5)
    c = _commitment(session, due_at=due, status="open")

    count = mark_stale_commitments(session)
    assert count == 1

    session.refresh(c)
    assert c.status == "stale"


def test_not_yet_overdue_stays_open(session: Session):
    """due_at tomorrow → status remains 'open'."""
    from tbc_worker_commitments.stale import mark_stale_commitments

    due = datetime.now(UTC) + timedelta(days=1)
    c = _commitment(session, due_at=due, status="open")

    count = mark_stale_commitments(session)
    assert count == 0

    session.refresh(c)
    assert c.status == "open"
