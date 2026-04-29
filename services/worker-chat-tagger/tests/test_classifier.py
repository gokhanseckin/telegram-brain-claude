"""Classifier write-path tests using SQLite via conftest."""

from __future__ import annotations

from datetime import UTC, datetime

from tbc_common.config import settings
from tbc_common.db.models import Chat, Message
from tbc_worker_chat_tagger.classifier import (
    TagDecision,
    _write_decision,
    candidate_chats,
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


def test_candidate_chats_strict_involvement_rule(session, monkeypatch):
    """candidate_chats must include only chats with owner involvement in 180d.

    Verifies all three involvement signals (sender, @-mention, reply-to-owner)
    and excludes chats with no owner involvement.
    """
    monkeypatch.setattr(settings, "tg_owner_user_id", 100)
    monkeypatch.setattr(settings, "tg_owner_username", "owner")
    now = datetime.now(UTC)

    # Chat A: owner SENT a message → kept
    session.add(Chat(chat_id=1, type="private", title="A", tag_locked=False))
    session.add(Message(chat_id=1, message_id=1, sent_at=now, sender_id=100, text="hi", raw={}))

    # Chat B: someone @mentioned the owner → kept
    session.add(Chat(chat_id=2, type="group", title="B", tag_locked=False))
    session.add(
        Message(
            chat_id=2, message_id=1, sent_at=now, sender_id=200,
            text="hey @owner can you check this", raw={},
        )
    )

    # Chat C: owner sent msg, someone REPLIED to it → kept
    session.add(Chat(chat_id=3, type="group", title="C", tag_locked=False))
    session.add(Message(chat_id=3, message_id=1, sent_at=now, sender_id=100, text="hello", raw={}))
    session.add(
        Message(
            chat_id=3, message_id=2, sent_at=now, sender_id=200,
            text="ack", reply_to_id=1, raw={},
        )
    )

    # Chat D: silent group, no owner involvement at all → SKIPPED
    session.add(Chat(chat_id=4, type="group", title="D", tag_locked=False))
    for i in range(5):
        session.add(
            Message(chat_id=4, message_id=i + 1, sent_at=now, sender_id=999, text="spam", raw={})
        )

    # Chat E: already tagged → excluded by tag IS NULL clause
    session.add(
        Chat(
            chat_id=5,
            type="private",
            title="E",
            tag="client",
            tag_locked=False,
        )
    )
    session.add(Message(chat_id=5, message_id=1, sent_at=now, sender_id=100, text="hi", raw={}))

    session.commit()

    kept = {c.chat_id for c in candidate_chats(session)}
    assert kept == {1, 2, 3}


def test_candidate_chats_skips_run_when_owner_unset(session, monkeypatch):
    """Without owner config, the strict rule can't be applied — return empty."""
    monkeypatch.setattr(settings, "tg_owner_user_id", None)
    monkeypatch.setattr(settings, "tg_owner_username", None)

    session.add(Chat(chat_id=1, type="private", title="A", tag_locked=False))
    session.commit()

    assert candidate_chats(session) == []
