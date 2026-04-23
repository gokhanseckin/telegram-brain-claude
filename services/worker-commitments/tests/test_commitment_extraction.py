"""Tests for commitment extraction job.

The `session` fixture is provided by ../conftest.py (SQLite in-memory).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session


def _mu(session, chat_id=1, message_id=1, is_commitment=True, commitment=None,
        is_signal=False, signal_type=None, signal_strength=None):
    from tbc_common.db.models import MessageUnderstanding

    mu = MessageUnderstanding(
        chat_id=chat_id,
        message_id=message_id,
        model_version="test-v1",
        is_commitment=is_commitment,
        commitment=commitment,
        is_signal=is_signal,
        signal_type=signal_type,
        signal_strength=signal_strength,
        processed_at=datetime.now(UTC),
        summary_en="Test summary",
    )
    session.add(mu)
    session.commit()
    return mu


def test_is_commitment_creates_row(session: Session):
    """is_commitment=True with commitment blob → commitments row created."""
    from tbc_common.db.models import Commitment
    from tbc_worker_commitments.extractor import extract_commitments

    _mu(
        session,
        chat_id=10,
        message_id=1,
        is_commitment=True,
        commitment={
            "who": "user",
            "what": "Send the proposal by Friday",
            "due": "2026-05-01",
            "confidence": 4,
        },
    )

    count = extract_commitments(session)
    assert count == 1

    commitments = session.query(Commitment).all()
    assert len(commitments) == 1
    c = commitments[0]
    assert c.owner == "user"
    assert c.description == "Send the proposal by Friday"
    assert c.due_at is not None
    assert c.status == "open"


def test_duplicate_not_created(session: Session):
    """Running extractor twice on the same MU row → only one commitment created."""
    from tbc_common.db.models import Commitment
    from tbc_worker_commitments.extractor import extract_commitments

    _mu(session, chat_id=10, message_id=2, is_commitment=True,
        commitment={"who": "counterparty", "what": "Will send contract", "due": None, "confidence": 3})

    extract_commitments(session)
    extract_commitments(session)

    assert session.query(Commitment).count() == 1


def test_non_commitment_skipped(session: Session):
    """is_commitment=False → no commitments row."""
    from tbc_common.db.models import Commitment
    from tbc_worker_commitments.extractor import extract_commitments

    _mu(session, chat_id=10, message_id=3, is_commitment=False)

    extract_commitments(session)

    assert session.query(Commitment).count() == 0
