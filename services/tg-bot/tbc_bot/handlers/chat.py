"""Free-text DM handler: forwards messages to Claude with MCP tool access."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import structlog
from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import Message

from tbc_bot.agent import ask
from tbc_bot.guards import is_owner
from tbc_bot.router import match_rule
from tbc_bot.router.executors import (
    CommitmentLookupFailed,
    exec_commitment_cancel,
    exec_commitment_resolve,
    exec_feedback,
)
from tbc_bot.router.llm import classify as llm_classify

log = structlog.get_logger(__name__)

router = Router(name="chat")

# Per-chat conversation history: chat_id -> [{role, content}, ...]
_history: dict[int, list[dict[str, Any]]] = defaultdict(list)
_MAX_HISTORY = 20  # 10 turns (user + assistant pairs)


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    if not is_owner(message):
        return
    _history[message.chat.id].clear()
    await message.answer("Conversation history cleared.")


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    if not is_owner(message):
        return
    await message.answer("No such command. Please type /help for help.")


@router.message(F.text)
async def handle_text(message: Message) -> None:
    if not is_owner(message):
        return
    if not message.text or not message.bot:
        return

    chat_id = message.chat.id
    await message.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    history = _history[chat_id]

    # ─── Router pre-pass ────────────────────────────────────────────────
    # 1) Rules first — strict-vocab regex catches obvious feedback DMs
    #    sub-second, no LLM, no Claude.
    decision = match_rule(message.text)

    # 2) LLM classifier — Qwen 3B handles free-text reactions, commitments,
    #    Q&A discrimination. Returns ambiguous on any failure (parse,
    #    schema, low confidence) so we never silently escalate.
    if decision is None:
        decision = await llm_classify(message.text)

    # 3) Dispatch.
    if decision.intent == "feedback":
        try:
            reply = await exec_feedback(decision)
        except Exception:
            log.exception(
                "router_executor_error", chat_id=chat_id, intent=decision.intent
            )
            await message.answer("Something went wrong, please try again.")
            return
        history.append({"role": "user", "content": message.text})
        history.append({"role": "assistant", "content": reply})
        if len(history) > _MAX_HISTORY:
            _history[chat_id] = history[-_MAX_HISTORY:]
        await message.answer(reply, parse_mode=None)
        log.info(
            "router_dispatch",
            intent=decision.intent,
            source=decision.source,
            confidence=decision.confidence,
            claude_called=False,
        )
        return

    # Commitment shortcuts: only the rule path carries an explicit
    # `commitment_id` (parsed from `done c42` / `cancel c42`). LLM-
    # classified commitment intents lack the id and still fall through
    # to Claude, which uses MCP get_commitments to find the right row.
    if (
        decision.intent in ("commitment_resolve", "commitment_cancel")
        and decision.source == "rule"
        and "commitment_id" in decision.fields
    ):
        try:
            if decision.intent == "commitment_resolve":
                reply = await exec_commitment_resolve(
                    decision, source_message_id=message.message_id
                )
            else:
                reply = await exec_commitment_cancel(decision)
        except CommitmentLookupFailed as exc:
            await message.answer(str(exc))
            log.info(
                "router_dispatch",
                intent=decision.intent,
                source=decision.source,
                claude_called=False,
                error="commitment_not_found",
            )
            return
        except Exception:
            log.exception(
                "router_executor_error", chat_id=chat_id, intent=decision.intent
            )
            await message.answer("Something went wrong, please try again.")
            return
        history.append({"role": "user", "content": message.text})
        history.append({"role": "assistant", "content": reply})
        if len(history) > _MAX_HISTORY:
            _history[chat_id] = history[-_MAX_HISTORY:]
        await message.answer(reply, parse_mode=None)
        log.info(
            "router_dispatch",
            intent=decision.intent,
            source=decision.source,
            confidence=decision.confidence,
            claude_called=False,
        )
        return

    if decision.intent == "ambiguous":
        # Loop guard: never escalate to Claude on a failed classification.
        # Make the user disambiguate instead — that's a fresh DM with a
        # fresh budget.
        reply = (
            "I wasn't sure how to handle that. Could you rephrase? "
            "For brief feedback try `#xxxx useful` (or not_useful / missed). "
            "For a question, just ask plainly."
        )
        history.append({"role": "user", "content": message.text})
        history.append({"role": "assistant", "content": reply})
        if len(history) > _MAX_HISTORY:
            _history[chat_id] = history[-_MAX_HISTORY:]
        await message.answer(reply, parse_mode=None)
        log.info(
            "router_dispatch",
            intent="ambiguous",
            source=decision.source,
            confidence=decision.confidence,
            claude_called=False,
            error=decision.fields.get("error"),
        )
        return

    # qa, commitment_resolve, commitment_cancel, commitment_update —
    # fall through to Claude. PR3 will replace the commitment fall-throughs
    # with local executors + echo-back; for now Claude handles them
    # correctly via the existing MCP tools.
    user_text = f"{message.text}\n\n[meta] current_message_id={message.message_id}"

    try:
        reply = await ask(history, user_text)
    except Exception:
        log.exception("agent_error", chat_id=chat_id)
        await message.answer("Something went wrong, please try again.")
        return

    history.append({"role": "user", "content": message.text})
    history.append({"role": "assistant", "content": reply})

    if len(history) > _MAX_HISTORY:
        _history[chat_id] = history[-_MAX_HISTORY:]

    log.info(
        "router_dispatch",
        intent=decision.intent,
        source=decision.source,
        confidence=decision.confidence,
        claude_called=True,
    )

    for chunk in _split(reply, 4096):
        await message.answer(chunk, parse_mode=None)


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks
