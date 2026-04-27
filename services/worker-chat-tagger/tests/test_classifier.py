"""Classifier write-path tests using SQLite via conftest."""

from __future__ import annotations

from datetime import UTC, datetime

from tbc_common.db.models import Chat, Message
from tbc_worker_chat_tagger.classifier import (
    TagDecision,
    _has_min_messages,
    _write_decision,
)


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


def test_has_min_messages_counts_raw_messages(session):
    """Stage B must be reachable for chats with raw text but no embeddings."""
    chat = Chat(chat_id=10, type="private", title="UntaggedRaw", tag_locked=False)
    session.add(chat)
    for i in range(12):
        session.add(
            Message(
                chat_id=10,
                message_id=i + 1,
                sent_at=datetime.now(UTC),
                text=f"hello {i}",
                raw={},
            )
        )
    session.commit()

    assert _has_min_messages(session, 10, floor=10) is True
    assert _has_min_messages(session, 10, floor=20) is False


def test_has_min_messages_skips_empty_text(session):
    """Empty-text or deleted messages do not count."""
    chat = Chat(chat_id=11, type="private", title="MostlyEmpty", tag_locked=False)
    session.add(chat)
    # 5 with real text, 5 with empty/null, 5 deleted — only 5 should count.
    for i in range(5):
        session.add(Message(chat_id=11, message_id=i + 1, sent_at=datetime.now(UTC), text="real", raw={}))
    for i in range(5):
        session.add(Message(chat_id=11, message_id=10 + i, sent_at=datetime.now(UTC), text="", raw={}))
    for i in range(5):
        session.add(
            Message(
                chat_id=11,
                message_id=20 + i,
                sent_at=datetime.now(UTC),
                text="trashed",
                deleted_at=datetime.now(UTC),
                raw={},
            )
        )
    session.commit()

    assert _has_min_messages(session, 11, floor=10) is False
    assert _has_min_messages(session, 11, floor=5) is True


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
