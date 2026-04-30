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
from tbc_bot.router.executors import exec_feedback

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

    # Router pre-pass — local-first dispatch. If a rule matches an
    # action-shaped DM (currently: explicit-sentiment feedback like
    # "#abcd useful"), the executor writes the row and we return without
    # ever calling Claude. Anything not matched falls through.
    decision = match_rule(message.text)
    if decision is not None and decision.intent == "feedback":
        try:
            reply = await exec_feedback(decision)
        except Exception:
            log.exception("router_executor_error", chat_id=chat_id, intent=decision.intent)
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
            claude_called=False,
        )
        return

    # Fall-through: no rule matched. Send to Claude exactly once.
    # Surface the Telegram message id to the agent so commitment resolutions
    # can be traced back to the exact DM that triggered them.
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

    log.info("router_dispatch", intent="qa", source="fallthrough", claude_called=True)

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
