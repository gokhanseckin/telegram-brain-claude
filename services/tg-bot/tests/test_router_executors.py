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
