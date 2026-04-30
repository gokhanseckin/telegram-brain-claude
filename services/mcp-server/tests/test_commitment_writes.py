"""Write-path tests for resolve / cancel / update commitment tools."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tbc_common.db.models import Commitment
from tbc_mcp_server.tools.commitments import (
    CommitmentNotFound,
    cancel_commitment,
    get_commitments,
    resolve_commitment,
    update_commitment,
)


def _seed(session, **overrides):
    c = Commitment(
        chat_id=overrides.get("chat_id", 1),
        source_message_id=overrides.get("source_message_id", 100),
        owner=overrides.get("owner", "user"),
        description=overrides.get("description", "send the report to Bob"),
        due_at=overrides.get("due_at"),
        source_sent_at=overrides.get("source_sent_at"),
        status=overrides.get("status", "open"),
    )
    session.add(c)
    session.commit()
    return c


def test_resolve_commitment_marks_done_and_records_note(session):
    c = _seed(session)
    result = resolve_commitment(
        session, commitment_id=c.id, note="sent today", resolved_by_message_id=999
    )
    assert result.status == "done"
    assert result.resolved_at is not None
    assert result.resolved_by_message_id == 999
    assert "sent today" in result.description
    assert "[resolved" in result.description


def test_resolve_commitment_without_note_still_works(session):
    c = _seed(session, description="ping Alice")
    result = resolve_commitment(session, commitment_id=c.id)
    assert result.status == "done"
    assert "[resolved" in result.description
    # Original wording must remain
    assert result.description.startswith("ping Alice")


def test_resolve_commitment_unknown_id_raises(session):
    with pytest.raises(CommitmentNotFound):
        resolve_commitment(session, commitment_id=99999)


def test_cancel_commitment_marks_cancelled(session):
    c = _seed(session, description="follow up with vendor")
    result = cancel_commitment(session, commitment_id=c.id, reason="no longer needed")
    assert result.status == "cancelled"
    assert result.resolved_at is not None
    assert "no longer needed" in result.description
    assert "[cancelled" in result.description


def test_update_commitment_sets_due_at(session):
    c = _seed(session)
    new_due = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    result = update_commitment(session, commitment_id=c.id, due_at=new_due)
    assert result.status == "open"  # not closed
    # SQLite strips tzinfo on round-trip; compare wall-clock fields
    assert result.due_at.replace(tzinfo=None) == new_due.replace(tzinfo=None)


def test_update_commitment_appends_note(session):
    c = _seed(session, description="contract with Acme")
    result = update_commitment(
        session, commitment_id=c.id, note_append="waiting on Bob's reply"
    )
    assert result.status == "open"
    assert result.description.startswith("contract with Acme")
    assert "waiting on Bob's reply" in result.description
    assert "[note" in result.description


def test_update_commitment_requires_at_least_one_field(session):
    c = _seed(session)
    with pytest.raises(ValueError):
        update_commitment(session, commitment_id=c.id)


def test_get_commitments_query_filters_by_substring(session):
    _seed(session, description="send the report to Bob")
    _seed(session, source_message_id=101, description="pay Gizem $67.05")
    _seed(session, source_message_id=102, description="set up call with Alice")

    found = get_commitments(session, status="open", query="gizem")
    assert len(found) == 1
    assert "Gizem" in found[0].description

    # Case-insensitive
    found_upper = get_commitments(session, status="open", query="GIZEM")
    assert len(found_upper) == 1

    # No match
    found_none = get_commitments(session, status="open", query="banana")
    assert found_none == []


def test_get_commitments_respects_limit(session):
    for i in range(10):
        _seed(session, source_message_id=200 + i, description=f"task {i}")

    found = get_commitments(session, status="open", limit=3)
    assert len(found) == 3


def test_get_commitments_ids_lookup(session):
    """`ids=[...]` direct-lookup path — used for `c<id>` references."""
    a = _seed(session, source_message_id=300, description="alpha task")
    b = _seed(session, source_message_id=301, description="beta task")
    _seed(session, source_message_id=302, description="gamma task")

    found = get_commitments(session, ids=[a.id, b.id])
    assert {c.id for c in found} == {a.id, b.id}

    # Single id works too.
    found_one = get_commitments(session, ids=[a.id])
    assert [c.id for c in found_one] == [a.id]

    # Unknown id returns empty (no error).
    assert get_commitments(session, ids=[999_999]) == []


def test_get_commitments_ids_combines_with_status(session):
    """ids + status both apply — caller can scope the lookup."""
    open_c = _seed(session, source_message_id=310, description="open one")
    done_c = _seed(session, source_message_id=311, description="done one")
    resolve_commitment(session, commitment_id=done_c.id)

    # ids alone returns both regardless of status
    both = get_commitments(session, ids=[open_c.id, done_c.id])
    assert {c.id for c in both} == {open_c.id, done_c.id}

    # ids + status="open" filters to just the open one
    only_open = get_commitments(session, ids=[open_c.id, done_c.id], status="open")
    assert [c.id for c in only_open] == [open_c.id]
