"""Tests for the noise filter — chats filtered by tag in poll query.

Uses the SQLite-compatible models defined in conftest.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tbc_common.prompts import MODEL_VERSION

from .conftest import Chat, Message

# The poll SQL mirrors main.py exactly.
_POLL_SQL = text("""
    SELECT m.chat_id, m.message_id
    FROM messages m
    LEFT JOIN message_understanding mu
      ON mu.chat_id = m.chat_id
     AND mu.message_id = m.message_id
     AND mu.model_version = :model_version
    WHERE mu.message_id IS NULL
      AND m.deleted_at IS NULL
      AND m.text IS NOT NULL
      AND m.text != ''
      AND m.chat_id IN (
          SELECT chat_id FROM chats
          WHERE tag IS NOT NULL AND tag != 'ignore'
      )
    ORDER BY m.sent_at ASC
""")


def _insert_chat_and_message(
    session: Session, chat_id: int, tag: str | None, message_id: int = 1
) -> tuple[Chat, Message]:
    chat = Chat(chat_id=chat_id, type="private", tag=tag)
    msg = Message(
        chat_id=chat_id,
        message_id=message_id,
        sent_at=datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
        text="Some business message",
        raw={},
    )
    session.add(chat)
    session.add(msg)
    session.commit()
    return chat, msg


def _run_poll(session: Session) -> list[tuple[int, int]]:
    rows = session.execute(_POLL_SQL, {"model_version": MODEL_VERSION}).fetchall()
    return [(r.chat_id, r.message_id) for r in rows]


def test_ignored_chat_not_processed(db_session: Session) -> None:
    _insert_chat_and_message(db_session, chat_id=2001, tag="ignore")
    results = _run_poll(db_session)
    assert (2001, 1) not in results


def test_untagged_chat_not_processed(db_session: Session) -> None:
    _insert_chat_and_message(db_session, chat_id=2002, tag=None)
    results = _run_poll(db_session)
    assert (2002, 1) not in results


def test_tagged_chat_is_processed(db_session: Session) -> None:
    _insert_chat_and_message(db_session, chat_id=2003, tag="client")
    results = _run_poll(db_session)
    assert (2003, 1) in results
