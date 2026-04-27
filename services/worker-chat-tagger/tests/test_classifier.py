"""Classifier write-path tests using SQLite via conftest."""

from __future__ import annotations

from datetime import UTC, datetime

from tbc_common.db.models import Chat
from tbc_worker_chat_tagger.classifier import TagDecision, _write_decision


def test_write_decision_updates_chat(session):
    chat = Chat(chat_id=1, type="private", title="ACME", tag_locked=False)
    session.add(chat)
    session.commit()

    decision = TagDecision(
        tag="supplier", confidence=0.91, source="auto_llm", reason="buys parts"
    )
    _write_decision(session, chat, decision)

    session.expire_all()
    refreshed = session.get(Chat,1)
    assert refreshed.tag == "supplier"
    assert refreshed.tag_source == "auto_llm"
    assert abs(refreshed.tag_confidence - 0.91) < 1e-6
    assert refreshed.tag_reason == "buys parts"
    assert refreshed.tag_set_at is not None


def test_write_decision_skips_locked_chat(session):
    chat = Chat(
        chat_id=2,
        type="private",
        title="Mom",
        tag="family",
        tag_source="manual",
        tag_locked=True,
        tag_set_at=datetime.now(UTC),
    )
    session.add(chat)
    session.commit()

    decision = TagDecision(
        tag="client", confidence=0.99, source="auto_llm", reason="oops"
    )
    _write_decision(session, chat, decision)

    session.expire_all()
    refreshed = session.get(Chat,2)
    assert refreshed.tag == "family"  # unchanged
    assert refreshed.tag_source == "manual"
