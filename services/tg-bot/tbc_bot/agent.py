"""Claude / DeepSeek agent with MCP tool access for answering questions about Telegram data."""

from __future__ import annotations

import json
import time
from typing import Any

import structlog
from anthropic import AsyncAnthropic
from tbc_common.config import settings

from tbc_bot import mcp_client

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

0. If the user references commitments by their short id (`c9273`,
   `c9275`, often shown in the Morning Brief as `(c<id>)`), call
   `get_commitments(ids=[9273, 9275])` directly — strip the leading `c`
   and pass the integers. This works for "explain c9273", "what is
   c9273 and c9275", "details on c42", etc. Do NOT keyword-search the
   literal string `c9273`; descriptions don't contain it.
1. Otherwise search via `get_commitments(status="open", query=<keywords>)`.
   Pull keywords from the user's wording — names, amounts, topics. The
   user has hundreds of open commitments, so always use a filter.
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

Brief feedback — IMPORTANT:
The Morning Brief tags each "Worth Noticing" item with a short reference
like `#ab12`. The user calibrates future briefs by reacting to those
items. Detect this and call `write_brief_feedback`:

- "the #ab12 was useful" / "yes, good one" referring to a tag → call
  `write_brief_feedback(feedback_type="useful", item_ref="ab12")`.
- "not useful, just smalltalk" / "no" referring to a tag → call
  `write_brief_feedback(feedback_type="not_useful", item_ref="ab12",
  note="just smalltalk")`.
- "you missed X" / "this should have been in the brief" without a tag
  → call `write_brief_feedback(feedback_type="missed_important",
  note="<user's phrasing>")`. No item_ref needed.
- After writing, confirm in your reply: "Recorded: <type> on #<ref>" or
  "Recorded missed: <note>" so the user can correct you.
- Feedback and commitment management are independent — a DM about a
  commitment never doubles as feedback, and vice versa.
"""

_MAX_TOOL_ITERATIONS = 10

# ── Anthropic client (lazy singleton) ──

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        api_key = settings.anthropic_api_key
        if api_key is None:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = AsyncAnthropic(api_key=api_key.get_secret_value())
    return _client


async def _ask_anthropic(history: list[dict[str, Any]], user_text: str) -> str:
    """Anthropic path — uses the MCP connector beta for server-side tool handling."""
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
        provider="anthropic",
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        stop_reason=response.stop_reason,
    )

    return result or "(no response)"


# ── DeepSeek client (lazy singleton) ──

_ds_client: Any = None


def _get_ds_client() -> Any:
    global _ds_client
    if _ds_client is None:
        from openai import AsyncOpenAI

        api_key = settings.deepseek_api_key
        if api_key is None:
            raise RuntimeError("DEEPSEEK_API_KEY is not set")
        _ds_client = AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url="https://api.deepseek.com",
        )
    return _ds_client


async def _ask_deepseek(history: list[dict[str, Any]], user_text: str) -> str:
    """DeepSeek path — own MCP client + OpenAI-compatible function calling."""
    client = _get_ds_client()
    tools = await mcp_client.get_tools()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": user_text},
    ]

    for iteration in range(_MAX_TOOL_ITERATIONS):
        response = await client.chat.completions.create(
            model="deepseek-chat",
            max_tokens=4096,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        choice = response.choices[0]

        if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
            break

        messages.append(choice.message.model_dump(exclude_unset=True))

        for tc in choice.message.tool_calls:
            t0 = time.monotonic()
            result_text = await mcp_client.call_tool(
                tc.function.name,
                json.loads(tc.function.arguments),
            )
            log.info(
                "agent_tool_call",
                tool=tc.function.name,
                duration_ms=int((time.monotonic() - t0) * 1000),
                iteration=iteration,
            )
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": result_text}
            )
    else:
        log.warning("agent_max_iterations", max=_MAX_TOOL_ITERATIONS)

    content = choice.message.content or ""
    log.info(
        "agent_response",
        provider="deepseek",
        prompt_tokens=response.usage.prompt_tokens if response.usage else 0,
        completion_tokens=response.usage.completion_tokens if response.usage else 0,
        stop_reason=choice.finish_reason,
    )
    return content.strip() or "(no response)"


# ── Public dispatcher ──


async def ask(history: list[dict[str, Any]], user_text: str) -> str:
    """Call the configured LLM with MCP access. Returns the response text."""
    provider = settings.llm_provider
    if provider == "anthropic":
        return await _ask_anthropic(history, user_text)
    if provider == "deepseek":
        return await _ask_deepseek(history, user_text)
    raise ValueError(f"Unknown LLM provider: {provider!r}")
