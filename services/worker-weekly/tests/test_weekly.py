"""Tests for worker-weekly."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from tbc_common.prompts import WEEKLY_SYSTEM

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session_with_summaries(summaries: list) -> MagicMock:
    """Return a mock session that returns given summaries for ChatSummary queries."""
    session = MagicMock()

    # Build a mock for execute().all() that returns (summary, chat) tuples
    summary_results = []
    for s in summaries:
        mock_chat = MagicMock()
        mock_chat.title = s.get("chat_title", "Test Chat")
        mock_chat.chat_id = s.get("chat_id", 1)
        mock_summary = MagicMock()
        mock_summary.chat_id = s.get("chat_id", 1)
        mock_summary.period = s.get("period", "day")
        mock_summary.period_start = s.get("period_start", date(2026, 4, 15))
        mock_summary.summary = s.get("summary", "Test summary text")
        mock_summary.key_points = s.get("key_points", None)
        summary_results.append((mock_summary, mock_chat))

    # First call returns summaries (for ChatSummary join query)
    # Subsequent calls return empty lists
    call_count = [0]

    def execute_side_effect(*args, **kwargs):
        result = MagicMock()
        if call_count[0] == 0:
            result.all.return_value = summary_results
            result.scalars.return_value.all.return_value = []
        else:
            result.all.return_value = []
            result.scalars.return_value.all.return_value = []
        call_count[0] += 1
        return result

    session.execute.side_effect = execute_side_effect
    session.get.return_value = None
    return session


def _make_empty_session() -> MagicMock:
    session = MagicMock()
    session.execute.return_value.all.return_value = []
    session.execute.return_value.scalars.return_value.all.return_value = []
    session.get.return_value = None
    return session


# ---------------------------------------------------------------------------
# Test: weekly input assembles summaries
# ---------------------------------------------------------------------------

def test_weekly_input_assembles_summaries():
    """build_weekly_input includes chat summary text in the output."""
    from tbc_worker_weekly.assembler import build_weekly_input

    session = _make_session_with_summaries([
        {
            "chat_id": 1,
            "chat_title": "Acme Corp",
            "period": "day",
            "period_start": date(2026, 4, 15),
            "summary": "Discussed renewal pricing and next steps.",
        }
    ])

    result = build_weekly_input(session)

    assert "Acme Corp" in result
    assert "Discussed renewal pricing and next steps." in result
    assert "Daily Chat Summaries" in result


# ---------------------------------------------------------------------------
# Test: batch API called with correct params
# ---------------------------------------------------------------------------

def test_batch_api_called():
    """call_batch_api calls client.beta.messages.batches.create with correct model and WEEKLY_SYSTEM."""
    with patch("tbc_worker_weekly.sender.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Mock batch creation
        mock_batch = MagicMock()
        mock_batch.id = "batch-test-123"
        mock_batch.processing_status = "ended"
        mock_client.beta.messages.batches.create.return_value = mock_batch
        mock_client.beta.messages.batches.retrieve.return_value = mock_batch

        # Mock results
        mock_result = MagicMock()
        mock_result.result.type = "succeeded"
        mock_result.result.message.content = [MagicMock(text="Weekly review text")]
        mock_client.beta.messages.batches.results.return_value = [mock_result]

        with patch("tbc_worker_weekly.sender.settings") as mock_settings:
            mock_settings.anthropic_api_key = MagicMock()
            mock_settings.anthropic_api_key.get_secret_value.return_value = "test-key"
            mock_settings.brief_model = "claude-sonnet-4-6"

            with patch("tbc_worker_weekly.sender.time") as mock_time:
                mock_time.monotonic.side_effect = [0, 100]  # won't timeout
                mock_time.sleep = MagicMock()

                from tbc_worker_weekly.sender import call_batch_api
                result = call_batch_api("weekly input text", date(2026, 4, 22))

        assert result == "Weekly review text"

        create_call = mock_client.beta.messages.batches.create.call_args
        requests = create_call.kwargs["requests"]
        assert len(requests) == 1
        assert requests[0]["params"]["model"] == "claude-sonnet-4-6"
        assert requests[0]["params"]["system"] == WEEKLY_SYSTEM


# ---------------------------------------------------------------------------
# Test: weekly written to chat_summaries
# ---------------------------------------------------------------------------

def test_weekly_written_to_chat_summaries():
    """save_weekly writes a row with period='week' and chat_id=0."""
    from tbc_worker_weekly.sender import save_weekly

    session = MagicMock()
    monday = date(2026, 4, 21)

    save_weekly(session, "Weekly review content", monday)

    assert session.execute.called
    assert session.commit.called
