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

Commitment management — IMPORTANT:
The user can tell you in plain language that they completed, cancelled, or
want to change a commitment ("I sent the report today", "paid Gizem the
$67.05", "forget that thing about Bob", "push the contract to next Friday",
"add a note: waiting on Sara's reply"). When you detect this:

1. Search via `get_commitments(status="open", query=<keywords>)`. Pull
   keywords from the user's wording — names, amounts, topics. The user has
   hundreds of open commitments, so always use the query filter.
2. If exactly one clear match, call the appropriate write tool
   (`resolve_commitment`, `cancel_commitment`, `update_commitment`) and
   confirm in your reply: "Marked done: #<id> — <description>." Always cite
   the id so the user can correct you.
3. If multiple plausible matches, list 2-5 of them with id + description +
   age, and ask "Which one?" — never guess.
4. If no match, say so honestly. Never fabricate a commitment id.
5. When resolving, pass the user's wording as `note=...` so the audit trail
   captures their actual phrasing.
6. Never resolve or cancel without explicit user intent. A question about a
   commitment is not a resolution.

When the user message includes a "[meta] current_message_id=..." line,
pass that integer as `resolved_by_message_id` on `resolve_commitment` so
we can trace closures back to the exact DM that triggered them.
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
