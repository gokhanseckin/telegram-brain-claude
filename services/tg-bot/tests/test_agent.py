"""Tests for agent.py — dispatcher routing and DeepSeek tool loop."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_messages",
            "description": "Search messages",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


def _make_choice(*, content: str | None = "Hello", tool_calls: list | None = None, finish_reason: str = "stop"):
    msg = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        model_dump=lambda exclude_unset=False: {
            "role": "assistant",
            "content": content,
            **({"tool_calls": [tc._asdict() if hasattr(tc, "_asdict") else tc for tc in tool_calls]} if tool_calls else {}),
        },
    )
    return SimpleNamespace(message=msg, finish_reason=finish_reason)


def _make_response(choice, *, prompt_tokens: int = 10, completion_tokens: int = 5):
    return SimpleNamespace(
        choices=[choice],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def _make_tool_call(name: str = "search_messages", arguments: str = '{"query": "test"}', tc_id: str = "tc_1"):
    return SimpleNamespace(
        id=tc_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


@pytest.mark.asyncio
async def test_ask_routes_to_anthropic():
    with (
        patch("tbc_bot.agent.settings") as mock_settings,
        patch("tbc_bot.agent._ask_anthropic", new_callable=AsyncMock, return_value="anthropic answer") as mock_anthropic,
        patch("tbc_bot.agent._ask_deepseek", new_callable=AsyncMock) as mock_deepseek,
    ):
        mock_settings.llm_provider = "anthropic"
        from tbc_bot.agent import ask

        result = await ask([], "hi")
        assert result == "anthropic answer"
        mock_anthropic.assert_awaited_once()
        mock_deepseek.assert_not_awaited()


@pytest.mark.asyncio
async def test_ask_routes_to_deepseek():
    with (
        patch("tbc_bot.agent.settings") as mock_settings,
        patch("tbc_bot.agent._ask_anthropic", new_callable=AsyncMock) as mock_anthropic,
        patch("tbc_bot.agent._ask_deepseek", new_callable=AsyncMock, return_value="deepseek answer") as mock_deepseek,
    ):
        mock_settings.llm_provider = "deepseek"
        from tbc_bot.agent import ask

        result = await ask([], "hi")
        assert result == "deepseek answer"
        mock_deepseek.assert_awaited_once()
        mock_anthropic.assert_not_awaited()


@pytest.mark.asyncio
async def test_ask_unknown_provider_raises():
    with patch("tbc_bot.agent.settings") as mock_settings:
        mock_settings.llm_provider = "unknown"
        from tbc_bot.agent import ask

        with pytest.raises(ValueError, match="Unknown LLM provider"):
            await ask([], "hi")


@pytest.mark.asyncio
async def test_deepseek_no_tool_calls():
    choice = _make_choice(content="Simple answer", finish_reason="stop")
    response = _make_response(choice)

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=response)

    with (
        patch("tbc_bot.agent._get_ds_client", return_value=mock_client),
        patch("tbc_bot.agent.mcp_client") as mock_mcp,
    ):
        mock_mcp.get_tools = AsyncMock(return_value=SAMPLE_TOOLS)
        mock_mcp.call_tool = AsyncMock()

        from tbc_bot.agent import _ask_deepseek

        result = await _ask_deepseek([], "hello")
        assert result == "Simple answer"
        mock_mcp.call_tool.assert_not_awaited()
        mock_client.chat.completions.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_deepseek_single_tool_call():
    tc = _make_tool_call()
    first_choice = _make_choice(content=None, tool_calls=[tc], finish_reason="tool_calls")
    first_response = _make_response(first_choice)

    final_choice = _make_choice(content="Found 3 messages", finish_reason="stop")
    final_response = _make_response(final_choice)

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=[first_response, final_response])

    with (
        patch("tbc_bot.agent._get_ds_client", return_value=mock_client),
        patch("tbc_bot.agent.mcp_client") as mock_mcp,
    ):
        mock_mcp.get_tools = AsyncMock(return_value=SAMPLE_TOOLS)
        mock_mcp.call_tool = AsyncMock(return_value='[{"id": 1}]')

        from tbc_bot.agent import _ask_deepseek

        result = await _ask_deepseek([], "search test")
        assert result == "Found 3 messages"
        mock_mcp.call_tool.assert_awaited_once_with("search_messages", {"query": "test"})
        assert mock_client.chat.completions.create.await_count == 2


@pytest.mark.asyncio
async def test_deepseek_max_iterations():
    tc = _make_tool_call()
    loop_choice = _make_choice(content=None, tool_calls=[tc], finish_reason="tool_calls")
    loop_response = _make_response(loop_choice)

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=loop_response)

    with (
        patch("tbc_bot.agent._get_ds_client", return_value=mock_client),
        patch("tbc_bot.agent.mcp_client") as mock_mcp,
        patch("tbc_bot.agent._MAX_TOOL_ITERATIONS", 10),
    ):
        mock_mcp.get_tools = AsyncMock(return_value=SAMPLE_TOOLS)
        mock_mcp.call_tool = AsyncMock(return_value="result")

        from tbc_bot.agent import _ask_deepseek

        result = await _ask_deepseek([], "loop forever")
        assert mock_client.chat.completions.create.await_count == 10
        assert result == "(no response)"
