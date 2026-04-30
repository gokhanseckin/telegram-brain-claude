"""Integration tests for handlers/chat.py — proves the cost guardrail.

The hard property of the router design is: **at most one Claude call per
user DM**. PR1 enforces this by only sending the DM to `agent.ask()` on
the fall-through path; rule-matched DMs return before that line. These
tests assert the property by mocking `ask` and counting calls.
"""

from __future__ import annotations

from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

OWNER_ID = 42


def _make_message(text: str, message_id: int = 1, user_id: int = OWNER_ID) -> AsyncMock:
    msg = AsyncMock()
    msg.text = text
    msg.message_id = message_id
    msg.from_user = SimpleNamespace(id=user_id)
    msg.chat = SimpleNamespace(id=999)
    msg.bot = AsyncMock()
    msg.bot.send_chat_action = AsyncMock()
    msg.answer = AsyncMock()
    return msg


@pytest.fixture(autouse=True)
def patch_settings():
    with patch("tbc_common.config.settings") as mock_settings:
        mock_settings.tg_owner_user_id = OWNER_ID
        mock_settings.tg_bot_token = None
        mock_settings.database_url = "postgresql+psycopg://fake/fake"
        yield mock_settings


@pytest.fixture(autouse=True)
def reset_chat_history():
    """The chat handler keeps an in-process history dict. Reset between
    tests so one test's writes don't bleed into the next."""
    from tbc_bot.handlers import chat as chat_module
    chat_module._history = defaultdict(list)
    yield


@pytest.mark.asyncio
async def test_rule_match_writes_row_and_does_not_call_claude():
    """`#abcd useful` is a clean rule match — must dispatch to executor
    without ever touching Claude."""
    from tbc_bot.handlers.chat import handle_text

    msg = _make_message("#abcd useful")

    mock_ask = AsyncMock(return_value="should never be called")
    mock_exec = AsyncMock(return_value="Recorded: useful on #abcd (id=1).")

    with (
        patch("tbc_bot.handlers.chat.is_owner", return_value=True),
        patch("tbc_bot.handlers.chat.ask", mock_ask),
        patch("tbc_bot.handlers.chat.exec_feedback", mock_exec),
    ):
        await handle_text(msg)

    assert mock_ask.call_count == 0, "Claude must not be called on rule match"
    assert mock_exec.call_count == 1
    decision = mock_exec.call_args[0][0]
    assert decision.intent == "feedback"
    assert decision.fields["feedback_type"] == "useful"
    assert decision.fields["item_ref"] == "abcd"
    msg.answer.assert_called_once()


@pytest.mark.asyncio
async def test_no_rule_match_falls_through_to_claude_exactly_once():
    """A free-text DM with no rule match goes to Claude — exactly once."""
    from tbc_bot.handlers.chat import handle_text

    # The Doğa case from Stage 1 verification: tag + free-text reaction.
    # Rules intentionally don't catch it (sentiment isn't in the vocab),
    # so it must fall through.
    msg = _make_message("#a8ce Doğa is not a prospect, he is a friend")

    mock_ask = AsyncMock(return_value="Recorded.")
    mock_exec = AsyncMock(return_value="should never be called")

    with (
        patch("tbc_bot.handlers.chat.is_owner", return_value=True),
        patch("tbc_bot.handlers.chat.ask", mock_ask),
        patch("tbc_bot.handlers.chat.exec_feedback", mock_exec),
    ):
        await handle_text(msg)

    assert mock_ask.call_count == 1, "Claude must be called exactly once"
    assert mock_exec.call_count == 0, "executor must not run on fall-through"
    msg.answer.assert_called()


@pytest.mark.asyncio
async def test_qa_query_falls_through_to_claude():
    """Plain Q&A — rule doesn't match, falls through to Claude once."""
    from tbc_bot.handlers.chat import handle_text

    msg = _make_message("what did Alice say last week?")

    mock_ask = AsyncMock(return_value="Last Tuesday Alice said …")
    mock_exec = AsyncMock()

    with (
        patch("tbc_bot.handlers.chat.is_owner", return_value=True),
        patch("tbc_bot.handlers.chat.ask", mock_ask),
        patch("tbc_bot.handlers.chat.exec_feedback", mock_exec),
    ):
        await handle_text(msg)

    assert mock_ask.call_count == 1
    assert mock_exec.call_count == 0


@pytest.mark.asyncio
async def test_rule_match_executor_failure_does_not_silently_invoke_claude():
    """If the executor raises, we apologise to the user — we do NOT
    silently retry through Claude. That's the cost-guardrail invariant."""
    from tbc_bot.handlers.chat import handle_text

    msg = _make_message("#abcd useful")

    mock_ask = AsyncMock(return_value="should never be called")
    mock_exec = AsyncMock(side_effect=RuntimeError("db down"))

    with (
        patch("tbc_bot.handlers.chat.is_owner", return_value=True),
        patch("tbc_bot.handlers.chat.ask", mock_ask),
        patch("tbc_bot.handlers.chat.exec_feedback", mock_exec),
    ):
        await handle_text(msg)

    assert mock_ask.call_count == 0, "must NOT escalate to Claude on executor failure"
    msg.answer.assert_called_once()
    err_text = msg.answer.call_args[0][0]
    assert "wrong" in err_text.lower() or "try again" in err_text.lower()


@pytest.mark.asyncio
async def test_non_owner_message_ignored():
    """Guard test — non-owner DMs neither hit the router nor Claude."""
    from tbc_bot.handlers.chat import handle_text

    msg = _make_message("#abcd useful", user_id=999)

    mock_ask = AsyncMock()
    mock_exec = AsyncMock()

    with (
        patch("tbc_bot.handlers.chat.is_owner", return_value=False),
        patch("tbc_bot.handlers.chat.ask", mock_ask),
        patch("tbc_bot.handlers.chat.exec_feedback", mock_exec),
    ):
        await handle_text(msg)

    assert mock_ask.call_count == 0
    assert mock_exec.call_count == 0
    msg.answer.assert_not_called()
