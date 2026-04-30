"""Unit tests for the understanding processor (OllamaClient + DB fully mocked)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tbc_common.prompts import MODEL_VERSION

VALID_RESPONSE: dict[str, Any] = {
    "language": "en",
    "entities": [],
    "intent": "request",
    "is_directed_at_user": True,
    "is_commitment": False,
    "commitment": None,
    "is_signal": True,
    "signal_type": "buying",
    "signal_strength": 3,
    "sentiment_delta": 1,
    "summary_en": "Client wants to schedule a demo.",
}


def _make_message(chat_id: int = 1001, message_id: int = 1, text: str = "hello") -> MagicMock:
    msg = MagicMock()
    msg.chat_id = chat_id
    msg.message_id = message_id
    msg.text = text
    msg.sent_at = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    return msg


def _make_ollama_mock(chat_response: str, embed_vector: list[float] | None = None) -> MagicMock:
    mock = MagicMock()
    mock.chat = AsyncMock(return_value=chat_response)
    mock.embed = AsyncMock(return_value=embed_vector or [0.1] * 1024)
    return mock


def _make_session_mock() -> MagicMock:
    """Create a mock SQLAlchemy session."""
    session = MagicMock()
    # scalars().all() returns empty list (no prior context messages)
    session.execute.return_value.scalars.return_value.all.return_value = []
    return session


@pytest.mark.asyncio
async def test_valid_json_writes_row() -> None:
    """Valid JSON response should cause session.execute + session.commit."""
    from tbc_worker_understanding.processor import process_message

    message = _make_message()
    session = _make_session_mock()
    ollama = _make_ollama_mock(json.dumps(VALID_RESPONSE))

    with patch("tbc_worker_understanding.processor.pg_insert") as mock_insert:
        mock_stmt = MagicMock()
        mock_insert.return_value.values.return_value.on_conflict_do_update.return_value = mock_stmt

        await process_message(
            message=message,
            session=session,
            ollama=ollama,
            understanding_model="test-model",
            embedding_model="embed-model",
            system_prompt="test-system",
        )

    # session.execute and session.commit must have been called
    session.execute.assert_called()
    session.commit.assert_called_once()

    # Verify model_version was passed correctly
    insert_call_kwargs = mock_insert.return_value.values.call_args
    assert insert_call_kwargs is not None
    kwargs = insert_call_kwargs.kwargs
    assert kwargs["model_version"] == MODEL_VERSION
    assert kwargs["intent"] == "request"
    assert kwargs["is_signal"] is True


@pytest.mark.asyncio
async def test_malformed_json_persists_failed_row() -> None:
    """Malformed JSON writes a minimal row (embedding only) so the queue advances."""
    from tbc_worker_understanding.processor import process_message

    message = _make_message()
    session = _make_session_mock()
    ollama = _make_ollama_mock("not json at all {{{")

    with patch("tbc_worker_understanding.processor.pg_insert") as mock_insert:
        mock_stmt = MagicMock()
        mock_insert.return_value.values.return_value.on_conflict_do_update.return_value = mock_stmt

        await process_message(
            message=message,
            session=session,
            ollama=ollama,
            understanding_model="test-model",
            embedding_model="embed-model",
            system_prompt="test-system",
        )

    insert_call_kwargs = mock_insert.return_value.values.call_args.kwargs
    assert insert_call_kwargs["model_version"] == MODEL_VERSION
    assert "summary_en" not in insert_call_kwargs
    assert "is_commitment" not in insert_call_kwargs
    assert insert_call_kwargs["embedding"] is not None
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_markdown_fenced_json_is_parsed() -> None:
    """Responses wrapped in ```json ... ``` should still parse successfully."""
    from tbc_worker_understanding.processor import process_message

    fenced = "```json\n" + json.dumps(VALID_RESPONSE) + "\n```"
    message = _make_message()
    session = _make_session_mock()
    ollama = _make_ollama_mock(fenced)

    with patch("tbc_worker_understanding.processor.pg_insert") as mock_insert:
        mock_stmt = MagicMock()
        mock_insert.return_value.values.return_value.on_conflict_do_update.return_value = mock_stmt

        await process_message(
            message=message,
            session=session,
            ollama=ollama,
            understanding_model="test-model",
            embedding_model="embed-model",
            system_prompt="test-system",
        )

    kwargs = mock_insert.return_value.values.call_args.kwargs
    assert kwargs["model_version"] == MODEL_VERSION
    assert kwargs["intent"] == VALID_RESPONSE["intent"]
    assert kwargs["summary_en"] == VALID_RESPONSE["summary_en"]


@pytest.mark.asyncio
async def test_missing_signal_fields_default_null() -> None:
    """signal_type=null in response should be stored as None."""
    from tbc_worker_understanding.processor import process_message

    response = {**VALID_RESPONSE, "signal_type": None, "signal_strength": None}
    message = _make_message()
    session = _make_session_mock()
    ollama = _make_ollama_mock(json.dumps(response))

    with patch("tbc_worker_understanding.processor.pg_insert") as mock_insert:
        mock_stmt = MagicMock()
        mock_insert.return_value.values.return_value.on_conflict_do_update.return_value = mock_stmt

        await process_message(
            message=message,
            session=session,
            ollama=ollama,
            understanding_model="test-model",
            embedding_model="embed-model",
            system_prompt="test-system",
        )

    insert_call_kwargs = mock_insert.return_value.values.call_args.kwargs
    assert insert_call_kwargs["signal_type"] is None
    assert insert_call_kwargs["signal_strength"] is None


@pytest.mark.asyncio
async def test_reprocessing_updates_existing_row() -> None:
    """Re-processing should use ON CONFLICT DO UPDATE, passing new model_version."""
    from tbc_worker_understanding.processor import process_message

    updated_response = {**VALID_RESPONSE, "intent": "commitment"}
    message = _make_message()
    session = _make_session_mock()
    ollama = _make_ollama_mock(json.dumps(updated_response))

    with patch("tbc_worker_understanding.processor.pg_insert") as mock_insert:
        mock_stmt = MagicMock()
        mock_insert.return_value.values.return_value.on_conflict_do_update.return_value = mock_stmt

        await process_message(
            message=message,
            session=session,
            ollama=ollama,
            understanding_model="test-model",
            embedding_model="embed-model",
            system_prompt="test-system",
        )

    # Confirm on_conflict_do_update was called (not a bare insert)
    mock_insert.return_value.values.return_value.on_conflict_do_update.assert_called_once()
    on_conflict_kwargs = (
        mock_insert.return_value.values.return_value.on_conflict_do_update.call_args.kwargs
    )
    # The set_ dict should contain model_version and intent
    assert on_conflict_kwargs["set_"]["model_version"] == MODEL_VERSION
    assert on_conflict_kwargs["set_"]["intent"] == "commitment"
