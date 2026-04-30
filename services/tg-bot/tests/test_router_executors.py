"""Tests for the feedback executor.

Mocks the SQLAlchemy sessionmaker — same pattern used in test_commands.py
for /feedback. We're testing that the executor reads RouterDecision
fields correctly and writes a BriefFeedback row with the right column
shape, not testing SQLAlchemy itself.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from tbc_bot.router.decision import RouterDecision
from tbc_bot.router.executors import exec_feedback


def _decision(**fields) -> RouterDecision:
    return RouterDecision(
        intent="feedback",
        confidence=1.0,
        source="rule",
        fields=fields,
    )


@pytest.mark.asyncio
async def test_executor_writes_row_with_ref():
    captured: list = []

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)

    def fake_add(row):
        row.id = 42
        captured.append(row)

    mock_session.add = fake_add
    mock_session.commit = MagicMock()
    mock_session.refresh = MagicMock()
    mock_sm = MagicMock(return_value=mock_session)

    with patch("tbc_bot.router.executors.get_sessionmaker", return_value=mock_sm):
        reply = await exec_feedback(
            _decision(feedback_type="useful", item_ref="abcd", note=None)
        )

    assert len(captured) == 1
    row = captured[0]
    assert row.feedback == "useful"
    assert row.item_ref == "abcd"
    assert row.note is None
    assert "useful" in reply
    assert "#abcd" in reply
    assert "42" in reply  # row id surfaced for audit


@pytest.mark.asyncio
async def test_exec_commitment_resolve_writes_status_done():
    """Resolve executor calls the shared db.commitments.resolve_commitment
    and returns a one-line confirmation string with the canonical id."""
    from tbc_bot.router.executors import exec_commitment_resolve

    fake_row = MagicMock()
    fake_row.id = 42
    fake_row.description = "send the report to Bob\n[resolved 2026-04-30: ok]"

    with patch(
        "tbc_bot.router.executors._resolve_commitment_db", return_value=fake_row
    ) as mock_db:
        reply = await exec_commitment_resolve(
            RouterDecision(
                intent="commitment_resolve",
                confidence=1.0,
                source="rule",
                fields={"commitment_id": 42, "note": "sent today"},
            ),
            source_message_id=99,
        )

    mock_db.assert_called_once()
    # Args: (db_session, commitment_id=42, note="sent today", resolved_by_message_id=99)
    call_kwargs = mock_db.call_args.kwargs
    assert call_kwargs["commitment_id"] == 42
    assert call_kwargs["note"] == "sent today"
    assert call_kwargs["resolved_by_message_id"] == 99
    assert "Marked done: c42" in reply
    assert "send the report to Bob" in reply
    # Audit-annotation suffix line stripped from the reply
    assert "[resolved" not in reply


@pytest.mark.asyncio
async def test_exec_commitment_resolve_unknown_id_raises_lookup_failed():
    from tbc_bot.router.executors import (
        CommitmentLookupFailed,
        exec_commitment_resolve,
    )
    from tbc_common.db.commitments import CommitmentNotFound

    with patch(
        "tbc_bot.router.executors._resolve_commitment_db",
        side_effect=CommitmentNotFound("commitment 99999 not found"),
    ), pytest.raises(CommitmentLookupFailed) as exc_info:
        await exec_commitment_resolve(
            RouterDecision(
                intent="commitment_resolve",
                confidence=1.0,
                source="rule",
                fields={"commitment_id": 99999, "note": None},
            ),
            source_message_id=1,
        )
    assert "c99999" in str(exc_info.value)


@pytest.mark.asyncio
async def test_exec_commitment_cancel_writes_status_cancelled():
    from tbc_bot.router.executors import exec_commitment_cancel

    fake_row = MagicMock()
    fake_row.id = 7
    fake_row.description = "follow up with vendor\n[cancelled 2026-04-30: noisy]"

    with patch(
        "tbc_bot.router.executors._cancel_commitment_db", return_value=fake_row
    ) as mock_db:
        reply = await exec_commitment_cancel(
            RouterDecision(
                intent="commitment_cancel",
                confidence=1.0,
                source="rule",
                fields={"commitment_id": 7, "reason": "no longer needed"},
            )
        )

    mock_db.assert_called_once()
    assert mock_db.call_args.kwargs["commitment_id"] == 7
    assert mock_db.call_args.kwargs["reason"] == "no longer needed"
    assert "Cancelled: c7" in reply


@pytest.mark.asyncio
async def test_executor_writes_row_without_ref():
    """missed_important without a tag — note becomes the headline."""
    captured: list = []

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)

    def fake_add(row):
        row.id = 7
        captured.append(row)

    mock_session.add = fake_add
    mock_session.commit = MagicMock()
    mock_session.refresh = MagicMock()
    mock_sm = MagicMock(return_value=mock_session)

    with patch("tbc_bot.router.executors.get_sessionmaker", return_value=mock_sm):
        reply = await exec_feedback(
            _decision(
                feedback_type="missed_important",
                item_ref=None,
                note="Yuri exit deserved a callout",
            )
        )

    assert captured[0].feedback == "missed_important"
    assert captured[0].item_ref is None
    assert captured[0].note == "Yuri exit deserved a callout"
    assert "missed" in reply.lower()
    assert "Yuri" in reply
