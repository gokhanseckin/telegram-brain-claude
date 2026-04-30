"""Tests for mcp_client.py — tool caching, schema translation, call_tool."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import TextContent


def _make_tool(name: str = "search_messages", description: str = "Search messages"):
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    )


def _mock_session(*, tools=None, call_result=None):
    session = AsyncMock()
    if tools is not None:
        session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=tools))
    if call_result is not None:
        session.call_tool = AsyncMock(return_value=call_result)
    return session


@pytest.fixture(autouse=True)
def _clear_cache():
    import tbc_bot.mcp_client as mod
    mod._tools_cache = None
    yield
    mod._tools_cache = None


def test_tool_to_openai_schema():
    from tbc_bot.mcp_client import _tool_to_openai

    tool = _make_tool()
    result = _tool_to_openai(tool)

    assert result["type"] == "function"
    assert result["function"]["name"] == "search_messages"
    assert result["function"]["description"] == "Search messages"
    assert result["function"]["parameters"]["type"] == "object"
    assert "query" in result["function"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_get_tools_returns_openai_format():
    tools = [_make_tool(), _make_tool(name="list_chats", description="List chats")]
    session = _mock_session(tools=tools)

    with patch("tbc_bot.mcp_client._open_session", new_callable=AsyncMock, return_value=session):
        from tbc_bot.mcp_client import get_tools

        result = await get_tools()
        assert len(result) == 2
        assert result[0]["function"]["name"] == "search_messages"
        assert result[1]["function"]["name"] == "list_chats"
        session.list_tools.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_tools_caches():
    tools = [_make_tool()]
    session = _mock_session(tools=tools)

    with patch("tbc_bot.mcp_client._open_session", new_callable=AsyncMock, return_value=session):
        from tbc_bot.mcp_client import get_tools

        await get_tools()
        await get_tools()

        session.list_tools.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_tool_returns_text():
    call_result = SimpleNamespace(
        content=[TextContent(type="text", text='{"ok": true}')],
        isError=False,
    )
    session = _mock_session(call_result=call_result)

    with patch("tbc_bot.mcp_client._open_session", new_callable=AsyncMock, return_value=session):
        from tbc_bot.mcp_client import call_tool

        result = await call_tool("search_messages", {"query": "test"})
        assert result == '{"ok": true}'
        session.call_tool.assert_awaited_once_with("search_messages", {"query": "test"})
