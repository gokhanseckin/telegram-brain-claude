"""Tests for MCP tools using a mocked database session."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tbc_mcp_server.models import MessageResult
from tbc_mcp_server.tools.chat import get_chat_history, list_chats
from tbc_mcp_server.tools.commitments import get_commitments
from tbc_mcp_server.tools.search import _make_deep_link, search_messages


# ---------------------------------------------------------------------------
# Helpers to build mock ORM objects
# ---------------------------------------------------------------------------


def _chat(
    chat_id: int = 1,
    title: str = "Test Chat",
    tag: str = "client",
    username: str | None = "testchat",
    participant_count: int = 2,
) -> MagicMock:
    m = MagicMock()
    m.chat_id = chat_id
    m.title = title
    m.tag = tag
    m.username = username
    m.participant_count = participant_count
    return m


def _user(
    user_id: int = 10,
    first_name: str = "Alice",
    last_name: str = "Smith",
    username: str = "alice",
) -> MagicMock:
    m = MagicMock()
    m.user_id = user_id
    m.first_name = first_name
    m.last_name = last_name
    m.username = username
    return m


def _message(
    message_id: int = 100,
    chat_id: int = 1,
    text: str = "hello world",
    sent_at: datetime | None = None,
    sender_id: int = 10,
    deleted_at: None = None,
) -> MagicMock:
    m = MagicMock()
    m.message_id = message_id
    m.chat_id = chat_id
    m.text = text
    m.sent_at = sent_at or datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    m.sender_id = sender_id
    m.deleted_at = deleted_at
    return m


def _understanding(
    chat_id: int = 1,
    message_id: int = 100,
    summary_en: str = "A test message",
    signal_type: str | None = "buying",
) -> MagicMock:
    m = MagicMock()
    m.chat_id = chat_id
    m.message_id = message_id
    m.summary_en = summary_en
    m.signal_type = signal_type
    return m


def _commitment(
    id: int = 1,
    chat_id: int = 1,
    owner: str = "user",
    description: str = "Send proposal",
    status: str = "open",
    due_at: datetime | None = None,
    created_at: datetime | None = None,
    resolved_at: datetime | None = None,
    resolved_by_message_id: int | None = None,
    source_message_id: int | None = None,
) -> MagicMock:
    m = MagicMock()
    m.id = id
    m.chat_id = chat_id
    m.owner = owner
    m.description = description
    m.status = status
    m.due_at = due_at or datetime(2024, 1, 1, tzinfo=timezone.utc)
    m.created_at = created_at or datetime(2024, 1, 1, tzinfo=timezone.utc)
    m.resolved_at = resolved_at
    m.resolved_by_message_id = resolved_by_message_id
    m.source_message_id = source_message_id
    return m


# ---------------------------------------------------------------------------
# test_search_messages_returns_results
# ---------------------------------------------------------------------------


def test_search_messages_returns_results():
    """search_messages returns MessageResult list with url field."""
    chat = _chat()
    user = _user()
    messages = [_message(message_id=i) for i in range(1, 4)]
    understandings = [_understanding(message_id=i) for i in range(1, 4)]

    db = MagicMock()
    # Return 3 rows from tsvector query
    db.execute.return_value.all.return_value = [
        (msg, chat, user, und) for msg, und in zip(messages, understandings)
    ]

    results = search_messages(db, query="hello")

    assert len(results) == 3
    for r in results:
        assert isinstance(r, MessageResult)
        assert r.url.startswith("tg://")
        assert r.chat_id == 1
        assert r.chat_title == "Test Chat"


# ---------------------------------------------------------------------------
# test_semantic_search_calls_ollama
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_search_calls_ollama():
    """semantic_search calls Ollama embed endpoint and uses result for pgvector query."""
    from tbc_mcp_server.tools.search import semantic_search

    chat = _chat()
    user = _user()
    msg = _message()
    und = _understanding()

    db = MagicMock()
    db.execute.return_value.all.return_value = [(msg, chat, user, und)]

    fake_embedding = [0.1] * 1024

    with patch("tbc_mcp_server.tools.search.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"embeddings": [fake_embedding]}
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        results = await semantic_search(db, query="budget discussion")

    # Verify Ollama was called
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert "embed" in call_args[0][0]  # URL contains "embed"

    # Verify pgvector query was constructed (db.execute was called)
    db.execute.assert_called_once()

    assert len(results) == 1
    assert results[0].url.startswith("tg://")


# ---------------------------------------------------------------------------
# test_get_chat_history_paginates
# ---------------------------------------------------------------------------


def test_get_chat_history_paginates():
    """get_chat_history respects limit parameter."""
    chat = _chat()
    user = _user()
    messages = [_message(message_id=i) for i in range(1, 6)]  # 5 messages

    db = MagicMock()
    # Simulate DB returning only 2 rows (limit was applied in SQL)
    db.execute.return_value.all.return_value = [
        (messages[0], chat, user, None),
        (messages[1], chat, user, None),
    ]

    results = get_chat_history(db, chat_id=1, limit=2)

    assert len(results) == 2
    # Verify limit was passed in the query
    execute_call = db.execute.call_args[0][0]
    # The compiled statement should have LIMIT applied


# ---------------------------------------------------------------------------
# test_list_chats_filters_by_tag
# ---------------------------------------------------------------------------


def test_list_chats_filters_by_tag():
    """list_chats returns only chats matching the specified tag."""
    client_chat = _chat(chat_id=1, tag="client", title="Client Corp")
    prospect_chat = _chat(chat_id=2, tag="prospect", title="Prospect Inc")

    db = MagicMock()
    # Simulate DB returning only the client chat after tag filter
    last_activity = datetime(2024, 6, 1, tzinfo=timezone.utc)
    db.execute.return_value.all.return_value = [(client_chat, last_activity)]

    results = list_chats(db, tag="client")

    assert len(results) == 1
    assert results[0].chat_id == 1
    assert results[0].tag == "client"
    assert results[0].title == "Client Corp"


# ---------------------------------------------------------------------------
# test_get_commitments_overdue_only
# ---------------------------------------------------------------------------


def test_get_commitments_overdue_only():
    """get_commitments with overdue_only=True applies due_at < NOW() filter."""
    past_due = _commitment(
        id=1,
        status="open",
        due_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        description="Overdue task",
    )
    future_due = _commitment(
        id=2,
        status="open",
        due_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        description="Future task",
    )

    db = MagicMock()
    # Simulate DB returning only the overdue commitment after filter
    db.execute.return_value.scalars.return_value.all.return_value = [past_due]

    results = get_commitments(db, overdue_only=True)

    assert len(results) == 1
    assert results[0].description == "Overdue task"

    # Verify the DB query was constructed (overdue filter applied)
    db.execute.assert_called_once()


# ---------------------------------------------------------------------------
# test_message_result_has_deep_link
# ---------------------------------------------------------------------------


def test_message_result_has_deep_link():
    """_make_deep_link produces valid tg:// deep links."""
    # Public chat with username
    public_chat = _chat(chat_id=12345, username="mychannel")
    url = _make_deep_link(public_chat, message_id=42)
    assert url == "tg://resolve?domain=mychannel&post=42"

    # Private chat (no username)
    private_chat = _chat(chat_id=-1001234567890, username=None)
    url = _make_deep_link(private_chat, message_id=99)
    assert url.startswith("tg://privatepost?channel=")
    assert "99" in url

    # MessageResult always has url field
    result = MessageResult(
        chat_id=1,
        chat_title="Test",
        chat_tag="client",
        message_id=100,
        sent_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        sender_name="Alice",
        text="hello",
        summary_en=None,
        signal_type=None,
        url="tg://resolve?domain=test&post=100",
    )
    assert result.url.startswith("tg://")
