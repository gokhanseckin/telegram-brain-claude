"""Claude agent with MCP tool access for answering questions about Telegram data."""

from __future__ import annotations

from typing import Any

import structlog
from anthropic import AsyncAnthropic
from tbc_common.config import settings

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """\
You are a personal assistant with direct access to the user's Telegram data via tools.
Use tools to look up chats, messages, summaries, commitments, signals, and relationship states.
Answer concisely. Make multiple tool calls when needed to fully answer the question.
Today's date will be provided in queries where relevant.
"""

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        api_key = settings.anthropic_api_key
        if api_key is None:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = AsyncAnthropic(api_key=api_key.get_secret_value())
    return _client


async def ask(history: list[dict[str, Any]], user_text: str) -> str:
    """Call Claude with MCP access. Returns the response text."""
    client = _get_client()

    mcp_token = settings.mcp_bearer_token
    if mcp_token is None:
        raise RuntimeError("TBC_MCP_BEARER_TOKEN is not set")

    messages = [*history, {"role": "user", "content": user_text}]

    response = await client.beta.messages.create(
        model=settings.brief_model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=messages,  # type: ignore[arg-type]
        mcp_servers=[
            {
                "type": "url",
                "url": f"{settings.mcp_public_url}/mcp",
                "name": "telegram-brain",
                "authorization_token": mcp_token.get_secret_value(),
            }
        ],
        betas=["mcp-client-2025-04-04"],
    )

    text_parts = [block.text for block in response.content if hasattr(block, "text")]
    result = "\n".join(text_parts).strip()

    log.info(
        "agent_response",
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        stop_reason=response.stop_reason,
    )

    return result or "(no response)"
