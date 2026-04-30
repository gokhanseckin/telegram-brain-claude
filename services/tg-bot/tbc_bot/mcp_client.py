"""MCP client for the DeepSeek agent path.

Connects to the TBC MCP server via streamable-HTTP transport, lists tools
(cached after first fetch), and invokes them by name.  Tool schemas are
translated to OpenAI function-calling format for DeepSeek consumption.
"""

from __future__ import annotations

from typing import Any

import structlog
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent, Tool
from tbc_common.config import settings

log = structlog.get_logger(__name__)

_tools_cache: list[dict[str, Any]] | None = None


def _auth_headers() -> dict[str, str]:
    token = settings.mcp_bearer_token
    if token is None:
        raise RuntimeError("TBC_MCP_BEARER_TOKEN is not set")
    return {"Authorization": f"Bearer {token.get_secret_value()}"}


def _tool_to_openai(tool: Tool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }


async def _open_session() -> ClientSession:
    """Open a fresh MCP session. Caller must use ``async with``."""
    url = f"{settings.mcp_public_url}/mcp"
    transport_cm = streamablehttp_client(url, headers=_auth_headers())
    read, write, _ = await transport_cm.__aenter__()
    session = ClientSession(read, write)
    await session.__aenter__()
    await session.initialize()
    return session


async def get_tools() -> list[dict[str, Any]]:
    """Return MCP tools in OpenAI function-calling format (cached)."""
    global _tools_cache
    if _tools_cache is not None:
        return _tools_cache

    session = await _open_session()
    result = await session.list_tools()

    _tools_cache = [_tool_to_openai(t) for t in result.tools]
    log.info("mcp_tools_cached", count=len(_tools_cache))
    return _tools_cache


async def call_tool(name: str, arguments: dict[str, Any]) -> str:
    """Invoke an MCP tool and return its text content."""
    session = await _open_session()
    result = await session.call_tool(name, arguments)

    parts = [c.text for c in result.content if isinstance(c, TextContent)]
    return "\n".join(parts)
