"""Write-path tests for write_brief_feedback MCP tool."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from tbc_common.db.models import BriefFeedback
from tbc_mcp_server.tools.feedback import (
    InvalidFeedbackType,
    write_brief_feedback,
)


def _all(session) -> list[BriefFeedback]:
    return list(session.execute(select(BriefFeedback)).scalars())


def test_useful_with_ref_writes_row(session):
    result = write_brief_feedback(
        session, feedback_type="useful", item_ref="#ab12"
    )
    assert result.feedback == "useful"
    assert result.item_ref == "ab12"
    assert result.note is None
    assert result.brief_date == date.today()
    rows = _all(session)
    assert len(rows) == 1
    assert rows[0].feedback == "useful"


def test_not_useful_with_note(session):
    result = write_brief_feedback(
        session,
        feedback_type="not_useful",
        item_ref="ab12",
        note="just smalltalk",
    )
    assert result.feedback == "not_useful"
    assert result.item_ref == "ab12"
    assert result.note == "just smalltalk"


def test_missed_important_without_ref(session):
    result = write_brief_feedback(
        session,
        feedback_type="missed_important",
        note="acme mentioned budget twice",
    )
    assert result.feedback == "missed_important"
    assert result.item_ref is None
    assert result.note == "acme mentioned budget twice"


def test_item_ref_strips_hash_and_lowercases(session):
    result = write_brief_feedback(
        session, feedback_type="useful", item_ref="#AB12"
    )
    assert result.item_ref == "ab12"


def test_item_ref_blank_string_treated_as_none(session):
    result = write_brief_feedback(
        session, feedback_type="missed_important", item_ref="   ", note="hi"
    )
    assert result.item_ref is None


def test_note_whitespace_stripped(session):
    result = write_brief_feedback(
        session,
        feedback_type="useful",
        item_ref="ab12",
        note="   ",
    )
    assert result.note is None


def test_invalid_feedback_type_raises(session):
    with pytest.raises(InvalidFeedbackType):
        write_brief_feedback(session, feedback_type="bogus", item_ref="ab12")
    # Nothing persisted on failure
    assert _all(session) == []


def test_explicit_brief_date_used(session):
    custom = date(2026, 1, 15)
    result = write_brief_feedback(
        session,
        feedback_type="useful",
        item_ref="ab12",
        brief_date=custom,
    )
    assert result.brief_date == custom


def test_duplicate_feedback_creates_separate_rows(session):
    """No unique constraint — slash command also stacks rows. Match it."""
    write_brief_feedback(session, feedback_type="useful", item_ref="ab12")
    write_brief_feedback(session, feedback_type="useful", item_ref="ab12")
    rows = _all(session)
    assert len(rows) == 2


def test_row_shape_matches_slash_command_inserts(session):
    """The MCP tool and /feedback handler both target brief_feedback. Confirm
    column population is identical so the brief calibration query treats them
    the same."""
    result = write_brief_feedback(
        session,
        feedback_type="not_useful",
        item_ref="ab12",
        note="dup of yesterday",
    )
    row = session.get(BriefFeedback, result.id)
    assert row is not None
    assert row.brief_date == date.today()
    assert row.item_ref == "ab12"
    assert row.feedback == "not_useful"
    assert row.note == "dup of yesterday"
    assert row.created_at is not None
