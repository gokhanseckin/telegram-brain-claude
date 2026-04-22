"""Unit tests for NewMessage, MessageEdited, and MessageDeleted handlers.

Uses unittest.mock to patch the DB session; no real Postgres or Telethon
connection is required.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers to build fake Telethon objects
# ---------------------------------------------------------------------------


def make_fake_message(
    msg_id: int = 1,
    chat_id: int = 100,
    sender_id: int = 42,
    text: str = "hello",
    date: datetime | None = None,
    reply_to_msg_id: int | None = None,
    edit_date: datetime | None = None,
) -> MagicMock:
    """Return a MagicMock that quacks like a Telethon Message."""
    if date is None:
        date = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    msg = MagicMock()
    msg.id = msg_id
    msg.chat_id = chat_id
    msg.sender_id = sender_id
    msg.message = text
    msg.date = date
    msg.reply_to_msg_id = reply_to_msg_id
    msg.edit_date = edit_date
    msg.to_dict.return_value = {
        "id": msg_id,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "message": text,
        "date": date.isoformat(),
    }
    # reply_to attribute expected by gap_recovery
    msg.reply_to = None
    return msg


def make_fake_sender(
    user_id: int = 42,
    first_name: str = "Alice",
    last_name: str = "Smith",
    username: str = "alice",
) -> MagicMock:
    sender = MagicMock()
    sender.id = user_id
    sender.first_name = first_name
    sender.last_name = last_name
    sender.username = username
    sender.is_self = False
    return sender


def make_fake_chat(chat_id: int = 100, title: str = "Test Chat") -> MagicMock:
    from telethon.tl.types import Chat as TgChat  # type: ignore[attr-defined]
    chat = MagicMock(spec=TgChat)
    chat.id = chat_id
    chat.title = title
    chat.username = None
    return chat


# ---------------------------------------------------------------------------
# Tests for _handle_new_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_message_inserts_user_chat_and_message():
    """A NewMessage event should upsert the sender, upsert the chat, and insert the message."""
    from tbc_ingestion.handlers import _handle_new_message

    fake_msg = make_fake_message(msg_id=7, chat_id=200, sender_id=99, text="test msg")
    fake_sender = make_fake_sender(user_id=99)
    fake_chat = make_fake_chat(chat_id=200, title="My Chat")

    # Build a fake event
    event = MagicMock()
    event.message = fake_msg
    event.chat_id = 200
    event.get_sender = AsyncMock(return_value=fake_sender)
    event.get_chat = AsyncMock(return_value=fake_chat)

    # Build the mock session chain
    mock_session_instance = MagicMock()
    mock_session_instance.__enter__ = MagicMock(return_value=mock_session_instance)
    mock_session_instance.__exit__ = MagicMock(return_value=False)
    # session.get returns None → new rows will be added
    mock_session_instance.get.return_value = None

    mock_sessionmaker = MagicMock(return_value=mock_session_instance)

    with patch("tbc_ingestion.handlers.get_sessionmaker", return_value=mock_sessionmaker), \
         patch("tbc_ingestion.handlers.is_paused", return_value=False):
        await _handle_new_message(event)

    # session.add should have been called at least 3 times: User, Chat, Message
    add_calls = mock_session_instance.add.call_args_list
    assert len(add_calls) >= 3, f"Expected >=3 add() calls, got {len(add_calls)}"
    mock_session_instance.commit.assert_called_once()

    # Verify a Message row was added with the right data
    from tbc_common.db.models import Message, User, Chat

    added_types = [type(c.args[0]).__name__ for c in add_calls]
    assert "User" in added_types
    assert "Chat" in added_types
    assert "Message" in added_types

    # Find the Message add call and verify fields
    msg_call = next(c for c in add_calls if isinstance(c.args[0], Message))
    added_msg: Message = msg_call.args[0]
    assert added_msg.message_id == 7
    assert added_msg.chat_id == 200
    assert added_msg.sender_id == 99
    assert added_msg.text == "test msg"
    assert added_msg.raw == fake_msg.to_dict()


@pytest.mark.asyncio
async def test_new_message_skips_duplicate():
    """If a message already exists in the DB, no new Message row should be added."""
    from tbc_ingestion.handlers import _handle_new_message
    from tbc_common.db.models import Message

    fake_msg = make_fake_message(msg_id=7, chat_id=200, sender_id=99)
    fake_sender = make_fake_sender(user_id=99)
    fake_chat = make_fake_chat(chat_id=200)

    event = MagicMock()
    event.message = fake_msg
    event.chat_id = 200
    event.get_sender = AsyncMock(return_value=fake_sender)
    event.get_chat = AsyncMock(return_value=fake_chat)

    existing_msg = MagicMock(spec=Message)
    mock_session_instance = MagicMock()
    mock_session_instance.__enter__ = MagicMock(return_value=mock_session_instance)
    mock_session_instance.__exit__ = MagicMock(return_value=False)

    # Return None for User and Chat, but return an existing Message
    def fake_get(model_class, pk):
        if model_class is Message:
            return existing_msg
        return None

    mock_session_instance.get.side_effect = fake_get
    mock_sessionmaker = MagicMock(return_value=mock_session_instance)

    with patch("tbc_ingestion.handlers.get_sessionmaker", return_value=mock_sessionmaker), \
         patch("tbc_ingestion.handlers.is_paused", return_value=False):
        await _handle_new_message(event)

    # Only User and Chat adds, not Message
    add_calls = mock_session_instance.add.call_args_list
    added_types = [type(c.args[0]).__name__ for c in add_calls]
    assert "Message" not in added_types


# ---------------------------------------------------------------------------
# Tests for _handle_message_edited
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_edited_updates_text_and_appends_history():
    """Editing a message should update text/edited_at and append old text to edit_history."""
    from tbc_ingestion.handlers import _handle_message_edited
    from tbc_common.db.models import Message

    edit_time = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    fake_msg = make_fake_message(
        msg_id=5, chat_id=300, text="new text", edit_date=edit_time
    )

    event = MagicMock()
    event.message = fake_msg
    event.chat_id = 300
    event.get_sender = AsyncMock(return_value=make_fake_sender())
    event.get_chat = AsyncMock(return_value=make_fake_chat(chat_id=300))

    # Existing message in DB
    existing = MagicMock(spec=Message)
    existing.text = "old text"
    existing.edited_at = None
    existing.raw = {"id": 5, "message": "old text"}

    mock_session_instance = MagicMock()
    mock_session_instance.__enter__ = MagicMock(return_value=mock_session_instance)
    mock_session_instance.__exit__ = MagicMock(return_value=False)
    mock_session_instance.get.return_value = existing
    mock_sessionmaker = MagicMock(return_value=mock_session_instance)

    with patch("tbc_ingestion.handlers.get_sessionmaker", return_value=mock_sessionmaker), \
         patch("tbc_ingestion.handlers.is_paused", return_value=False):
        await _handle_message_edited(event)

    assert existing.text == "new text"
    assert existing.edited_at == edit_time
    assert "edit_history" in existing.raw
    assert existing.raw["edit_history"][0]["text"] == "old text"
    mock_session_instance.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Tests for _handle_message_deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_deleted_sets_deleted_at():
    """A MessageDeleted event should set deleted_at on the matching row."""
    from tbc_ingestion.handlers import _handle_message_deleted
    from tbc_common.db.models import Message

    event = MagicMock()
    event.chat_id = 400
    event.deleted_ids = [10, 11]

    existing_10 = MagicMock(spec=Message)
    existing_10.deleted_at = None
    existing_11 = MagicMock(spec=Message)
    existing_11.deleted_at = None

    def fake_get(model_class, pk):
        chat_id, msg_id = pk
        if msg_id == 10:
            return existing_10
        if msg_id == 11:
            return existing_11
        return None

    mock_session_instance = MagicMock()
    mock_session_instance.__enter__ = MagicMock(return_value=mock_session_instance)
    mock_session_instance.__exit__ = MagicMock(return_value=False)
    mock_session_instance.get.side_effect = fake_get
    mock_sessionmaker = MagicMock(return_value=mock_session_instance)

    with patch("tbc_ingestion.handlers.get_sessionmaker", return_value=mock_sessionmaker), \
         patch("tbc_ingestion.handlers.is_paused", return_value=False):
        await _handle_message_deleted(event)

    assert existing_10.deleted_at is not None
    assert existing_11.deleted_at is not None
    mock_session_instance.commit.assert_called_once()


@pytest.mark.asyncio
async def test_message_deleted_no_chat_id_skips_gracefully():
    """When chat_id is None (PM deletion), the handler should not crash."""
    from tbc_ingestion.handlers import _handle_message_deleted

    event = MagicMock()
    event.chat_id = None
    event.deleted_ids = [99]

    mock_session_instance = MagicMock()
    mock_session_instance.__enter__ = MagicMock(return_value=mock_session_instance)
    mock_session_instance.__exit__ = MagicMock(return_value=False)
    mock_sessionmaker = MagicMock(return_value=mock_session_instance)

    with patch("tbc_ingestion.handlers.get_sessionmaker", return_value=mock_sessionmaker), \
         patch("tbc_ingestion.handlers.is_paused", return_value=False):
        # Should not raise
        await _handle_message_deleted(event)

    mock_session_instance.get.assert_not_called()
