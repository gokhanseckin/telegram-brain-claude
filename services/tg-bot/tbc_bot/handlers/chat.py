"""Free-text DM handler: forwards messages to Claude with MCP tool access."""

from __future__ import annotations

from collections import defaultdict

import structlog
from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import Message
from tbc_bot.agent import ask
from tbc_bot.guards import is_owner

log = structlog.get_logger(__name__)

router = Router(name="chat")

# Per-chat conversation history: chat_id -> [{role, content}, ...]
_history: dict[int, list[dict]] = defaultdict(list)
_MAX_HISTORY = 20  # 10 turns (user + assistant pairs)


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    if not is_owner(message):
        return
    _history[message.chat.id].clear()
    await message.answer("Conversation history cleared.")


@router.message(F.text)
async def handle_text(message: Message) -> None:
    if not is_owner(message):
        return
    if not message.text or not message.bot:
        return

    chat_id = message.chat.id
    await message.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    history = _history[chat_id]

    try:
        reply = await ask(history, message.text)
    except Exception:
        log.exception("agent_error", chat_id=chat_id)
        await message.answer("Something went wrong, please try again.")
        return

    history.append({"role": "user", "content": message.text})
    history.append({"role": "assistant", "content": reply})

    if len(history) > _MAX_HISTORY:
        _history[chat_id] = history[-_MAX_HISTORY:]

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
