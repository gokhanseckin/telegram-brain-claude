"""Integration tests for handlers/chat.py — proves the cost guardrail.

The hard property of the router design is: **at most one Claude call per
user DM**. The handler enforces this by code shape:
  rule match -> exec_feedback,         ask() never called
  llm intent=feedback -> exec_feedback, ask() never called
  llm intent=ambiguous -> apology,     ask() never called
  llm intent=qa or commitment_*       -> ask() called exactly once

These tests assert that property by mocking `ask` + `llm_classify` and
counting calls.
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


def _decision(intent, **fields):
    """Build a RouterDecision for the LLM-mock return value."""
    from tbc_bot.router.decision import RouterDecision
    return RouterDecision(
        intent=intent, confidence=0.9, source="llm", fields=fields
    )


@pytest.mark.asyncio
async def test_rule_match_writes_row_and_does_not_call_claude():
    """`#abcd useful` is a clean rule match — must dispatch to executor
    without ever touching the LLM or Claude."""
    from tbc_bot.handlers.chat import handle_text

    msg = _make_message("#abcd useful")

    mock_ask = AsyncMock(return_value="should never be called")
    mock_exec = AsyncMock(return_value="Recorded: useful on #abcd (id=1).")
    mock_llm = AsyncMock(return_value=_decision("qa"))

    with (
        patch("tbc_bot.handlers.chat.is_owner", return_value=True),
        patch("tbc_bot.handlers.chat.ask", mock_ask),
        patch("tbc_bot.handlers.chat.exec_feedback", mock_exec),
        patch("tbc_bot.handlers.chat.llm_classify", mock_llm),
    ):
        await handle_text(msg)

    assert mock_ask.call_count == 0, "Claude must not be called on rule match"
    assert mock_llm.call_count == 0, "LLM must not be called on rule match"
    assert mock_exec.call_count == 1
    decision = mock_exec.call_args[0][0]
    assert decision.intent == "feedback"
    assert decision.fields["feedback_type"] == "useful"
    msg.answer.assert_called_once()


@pytest.mark.asyncio
async def test_llm_classifies_feedback_no_claude_call():
    """Doğa case: rule doesn't match, LLM classifies as feedback —
    executor runs, Claude does NOT."""
    from tbc_bot.handlers.chat import handle_text

    msg = _make_message("#a8ce Doğa is not a prospect, he is a friend")

    mock_ask = AsyncMock(return_value="should never be called")
    mock_exec = AsyncMock(return_value="Recorded: not_useful on #a8ce")
    mock_llm = AsyncMock(return_value=_decision(
        "feedback",
        feedback_type="not_useful",
        item_ref="a8ce",
        note="Doğa is not a prospect, he is a friend",
    ))

    with (
        patch("tbc_bot.handlers.chat.is_owner", return_value=True),
        patch("tbc_bot.handlers.chat.ask", mock_ask),
        patch("tbc_bot.handlers.chat.exec_feedback", mock_exec),
        patch("tbc_bot.handlers.chat.llm_classify", mock_llm),
    ):
        await handle_text(msg)

    assert mock_llm.call_count == 1
    assert mock_exec.call_count == 1
    assert mock_ask.call_count == 0, "Claude must not be called when LLM picks feedback"


@pytest.mark.asyncio
async def test_llm_classifies_qa_falls_through_to_claude():
    """LLM picks qa → Claude called exactly once."""
    from tbc_bot.handlers.chat import handle_text

    msg = _make_message("what did Alice say last week?")

    mock_ask = AsyncMock(return_value="Last Tuesday Alice said …")
    mock_exec = AsyncMock()
    mock_llm = AsyncMock(return_value=_decision("qa"))

    with (
        patch("tbc_bot.handlers.chat.is_owner", return_value=True),
        patch("tbc_bot.handlers.chat.ask", mock_ask),
        patch("tbc_bot.handlers.chat.exec_feedback", mock_exec),
        patch("tbc_bot.handlers.chat.llm_classify", mock_llm),
    ):
        await handle_text(msg)

    assert mock_llm.call_count == 1
    assert mock_ask.call_count == 1, "Claude must be called exactly once for qa"
    assert mock_exec.call_count == 0


@pytest.mark.asyncio
async def test_llm_classifies_commitment_falls_through_to_claude():
    """Commitment intents still go to Claude in PR2; PR3 will replace
    with local executors. Property to enforce now: exactly one Claude
    call, executor not invoked."""
    from tbc_bot.handlers.chat import handle_text

    msg = _make_message("done with the report to Bob")

    mock_ask = AsyncMock(return_value="Marked done: #42 — send report to Bob.")
    mock_exec = AsyncMock()
    mock_llm = AsyncMock(return_value=_decision("commitment_resolve", query="report Bob"))

    with (
        patch("tbc_bot.handlers.chat.is_owner", return_value=True),
        patch("tbc_bot.handlers.chat.ask", mock_ask),
        patch("tbc_bot.handlers.chat.exec_feedback", mock_exec),
        patch("tbc_bot.handlers.chat.llm_classify", mock_llm),
    ):
        await handle_text(msg)

    assert mock_ask.call_count == 1
    assert mock_exec.call_count == 0


@pytest.mark.asyncio
async def test_llm_ambiguous_does_not_call_claude():
    """Loop guard: ambiguous => apology message, NO Claude call. This is
    the load-bearing invariant — Qwen failures must never silently
    escalate."""
    from tbc_bot.handlers.chat import handle_text

    msg = _make_message("forget about it")

    mock_ask = AsyncMock(return_value="should never be called")
    mock_exec = AsyncMock()
    mock_llm = AsyncMock(return_value=_decision(
        "ambiguous", error="low_confidence"
    ))

    with (
        patch("tbc_bot.handlers.chat.is_owner", return_value=True),
        patch("tbc_bot.handlers.chat.ask", mock_ask),
        patch("tbc_bot.handlers.chat.exec_feedback", mock_exec),
        patch("tbc_bot.handlers.chat.llm_classify", mock_llm),
    ):
        await handle_text(msg)

    assert mock_ask.call_count == 0, "must NOT escalate ambiguous to Claude"
    assert mock_exec.call_count == 0
    msg.answer.assert_called_once()
    reply = msg.answer.call_args[0][0]
    assert "rephrase" in reply.lower() or "wasn't sure" in reply.lower()


@pytest.mark.asyncio
async def test_rule_match_executor_failure_does_not_silently_invoke_claude():
    """Rule matched but executor raises — apologise, do NOT escalate."""
    from tbc_bot.handlers.chat import handle_text

    msg = _make_message("#abcd useful")

    mock_ask = AsyncMock(return_value="should never be called")
    mock_exec = AsyncMock(side_effect=RuntimeError("db down"))
    mock_llm = AsyncMock()

    with (
        patch("tbc_bot.handlers.chat.is_owner", return_value=True),
        patch("tbc_bot.handlers.chat.ask", mock_ask),
        patch("tbc_bot.handlers.chat.exec_feedback", mock_exec),
        patch("tbc_bot.handlers.chat.llm_classify", mock_llm),
    ):
        await handle_text(msg)

    assert mock_ask.call_count == 0, "must NOT escalate to Claude on executor failure"
    assert mock_llm.call_count == 0, "must NOT call LLM after rule match either"
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
    mock_llm = AsyncMock()

    with (
        patch("tbc_bot.handlers.chat.is_owner", return_value=False),
        patch("tbc_bot.handlers.chat.ask", mock_ask),
        patch("tbc_bot.handlers.chat.exec_feedback", mock_exec),
        patch("tbc_bot.handlers.chat.llm_classify", mock_llm),
    ):
        await handle_text(msg)

    assert mock_ask.call_count == 0
    assert mock_exec.call_count == 0
    assert mock_llm.call_count == 0
    msg.answer.assert_not_called()
