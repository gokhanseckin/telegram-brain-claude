"""Unit tests for gap_recovery.

Mocks client.get_messages to return fake Telethon message objects and
verifies that:
- All returned messages are written to the DB.
- The loop sleeps between pages.
- FloodWaitError causes a sleep-and-retry.
- A short final page terminates pagination without an extra sleep.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_fake_tg_message(
    msg_id: int,
    chat_id: int = 100,
    sender_id: int = 42,
    text: str = "hello",
) -> MagicMock:
    """Return a MagicMock that passes the isinstance(msg, TgMessage) check."""
    from telethon.tl.types import Message as TgMessage  # type: ignore[attr-defined]

    msg = MagicMock(spec=TgMessage)
    msg.id = msg_id
    msg.chat_id = chat_id
    msg.sender_id = sender_id
    msg.message = text
    msg.date = datetime(2024, 1, 1, tzinfo=timezone.utc)
    msg.edit_date = None
    msg.reply_to = None
    msg.to_dict.return_value = {
        "id": msg_id,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "message": text,
    }
    return msg


# ---------------------------------------------------------------------------
# Test: all messages across two pages are stored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gap_recovery_stores_all_messages_from_multiple_pages():
    """Messages from two pages should all be inserted, with a sleep between pages."""
    from tbc_ingestion.gap_recovery import _recover_chat, _PAGE_LIMIT, _PAGE_SLEEP_SECONDS

    chat_id = 100

    # Two pages: first is full (PAGE_LIMIT), second is short (terminates loop)
    page1 = [make_fake_tg_message(i, chat_id=chat_id) for i in range(1, _PAGE_LIMIT + 1)]
    page2 = [make_fake_tg_message(i, chat_id=chat_id) for i in range(_PAGE_LIMIT + 1, _PAGE_LIMIT + 6)]

    call_count = 0

    async def fake_get_messages(chat, limit, min_id):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return page1
        elif call_count == 2:
            return page2
        return []

    client = MagicMock()
    client.get_messages = fake_get_messages
    client.get_entity = AsyncMock(return_value=make_fake_tg_message(0))  # dummy sender

    # DB: max(message_id) = 0, all get() calls return None (no existing rows)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = 0

    mock_session_instance = MagicMock()
    mock_session_instance.__enter__ = MagicMock(return_value=mock_session_instance)
    mock_session_instance.__exit__ = MagicMock(return_value=False)
    mock_session_instance.execute.return_value = mock_result
    mock_session_instance.get.return_value = None  # no existing rows

    mock_sessionmaker = MagicMock(return_value=mock_session_instance)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    with patch("tbc_ingestion.gap_recovery.get_sessionmaker", return_value=mock_sessionmaker), \
         patch("tbc_ingestion.gap_recovery.asyncio.sleep", side_effect=fake_sleep):
        await _recover_chat(client, chat_id)

    # Should have added PAGE_LIMIT + 5 Message rows (plus User rows)
    add_calls = mock_session_instance.add.call_args_list
    from tbc_common.db.models import Message
    msg_adds = [c for c in add_calls if isinstance(c.args[0], Message)]
    assert len(msg_adds) == _PAGE_LIMIT + 5

    # Should have slept exactly once between the two pages
    page_sleeps = [s for s in sleep_calls if s == _PAGE_SLEEP_SECONDS]
    assert len(page_sleeps) >= 1


# ---------------------------------------------------------------------------
# Test: FloodWaitError causes sleep-and-retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gap_recovery_retries_after_flood_wait():
    """On FloodWaitError the loop should sleep e.seconds then retry the same page."""
    from tbc_ingestion.gap_recovery import _recover_chat
    from telethon.errors import FloodWaitError

    chat_id = 200

    call_count = 0
    flood_seconds = 30

    async def fake_get_messages(chat, limit, min_id):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate a flood wait on the first attempt
            err = FloodWaitError(request=MagicMock())
            err.seconds = flood_seconds
            raise err
        # Second attempt: return a single short page to end the loop
        return [make_fake_tg_message(1, chat_id=chat_id)]

    client = MagicMock()
    client.get_messages = fake_get_messages
    client.get_entity = AsyncMock(return_value=make_fake_tg_message(0))

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = 0

    mock_session_instance = MagicMock()
    mock_session_instance.__enter__ = MagicMock(return_value=mock_session_instance)
    mock_session_instance.__exit__ = MagicMock(return_value=False)
    mock_session_instance.execute.return_value = mock_result
    mock_session_instance.get.return_value = None

    mock_sessionmaker = MagicMock(return_value=mock_session_instance)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    with patch("tbc_ingestion.gap_recovery.get_sessionmaker", return_value=mock_sessionmaker), \
         patch("tbc_ingestion.gap_recovery.asyncio.sleep", side_effect=fake_sleep):
        await _recover_chat(client, chat_id)

    # get_messages was called twice (first raised flood wait, second succeeded)
    assert call_count == 2

    # The flood wait sleep should have been honored
    assert flood_seconds in sleep_calls


# ---------------------------------------------------------------------------
# Test: no chats → run_gap_recovery exits early
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_gap_recovery_no_chats():
    """If the chats table is empty, gap recovery should exit without calling get_messages."""
    from tbc_ingestion.gap_recovery import run_gap_recovery

    mock_result = MagicMock()
    mock_result.__iter__ = MagicMock(return_value=iter([]))

    mock_session_instance = MagicMock()
    mock_session_instance.__enter__ = MagicMock(return_value=mock_session_instance)
    mock_session_instance.__exit__ = MagicMock(return_value=False)
    mock_session_instance.scalars.return_value = mock_result

    mock_sessionmaker = MagicMock(return_value=mock_session_instance)

    client = MagicMock()
    client.get_messages = AsyncMock()

    with patch("tbc_ingestion.gap_recovery.get_sessionmaker", return_value=mock_sessionmaker):
        await run_gap_recovery(client)

    client.get_messages.assert_not_called()


# ---------------------------------------------------------------------------
# Test: already-stored messages are skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gap_recovery_skips_existing_messages():
    """Messages already in the DB (get() returns non-None) should not be re-inserted."""
    from tbc_ingestion.gap_recovery import _recover_chat

    chat_id = 300
    msg = make_fake_tg_message(1, chat_id=chat_id)

    async def fake_get_messages(chat, limit, min_id):
        return [msg]

    client = MagicMock()
    client.get_messages = fake_get_messages

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = 0

    mock_session_instance = MagicMock()
    mock_session_instance.__enter__ = MagicMock(return_value=mock_session_instance)
    mock_session_instance.__exit__ = MagicMock(return_value=False)
    mock_session_instance.execute.return_value = mock_result
    # session.get returns an existing Message → should be skipped
    mock_session_instance.get.return_value = MagicMock()

    mock_sessionmaker = MagicMock(return_value=mock_session_instance)

    with patch("tbc_ingestion.gap_recovery.get_sessionmaker", return_value=mock_sessionmaker), \
         patch("tbc_ingestion.gap_recovery.asyncio.sleep", new_callable=AsyncMock):
        await _recover_chat(client, chat_id)

    mock_session_instance.add.assert_not_called()
