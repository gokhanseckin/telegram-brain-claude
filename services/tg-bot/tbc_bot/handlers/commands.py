"""Simple command handlers: /ignore, /brief, /weekly, /search, /pause, /resume, /status."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select
from tbc_common.config import settings
from tbc_common.db.models import Chat, MessageUnderstanding
from tbc_common.db.models import Message as TgMessage
from tbc_common.db.session import get_sessionmaker

from tbc_bot.guards import is_owner

log = structlog.get_logger(__name__)

router = Router(name="commands")

TRIGGER_BRIEF = Path("/tmp/tbc_trigger_brief")
TRIGGER_WEEKLY = Path("/tmp/tbc_trigger_weekly")
PAUSE_FILE = Path("/tmp/tbc_pause")


@router.message(Command("ignore"))
async def cmd_ignore(message: Message) -> None:
    if not is_owner(message):
        return

    args = (message.text or "").split(maxsplit=1)
    chat_name = args[1].strip() if len(args) > 1 else None

    now = datetime.now(UTC)
    sm = get_sessionmaker()

    if chat_name:
        with sm() as session:
            stmt = select(Chat).where(Chat.title.ilike(f"%{chat_name}%")).limit(1)
            chat = session.scalars(stmt).first()
            if chat:
                chat.tag = "ignore"
                chat.tag_set_at = now
                chat.tag_source = "manual"
                chat.tag_locked = True
                chat.tag_confidence = None
                chat.tag_reason = None
                session.commit()
                await message.answer(f"Marked '{chat.title}' as ignored.")
            else:
                await message.answer(f"No chat found matching '{chat_name}'.")
    else:
        await message.answer(
            "To ignore a chat, use: /ignore ChatName\n"
            "(Direct chat context is not available in polling mode.)"
        )


@router.message(Command("brief"))
async def cmd_brief(message: Message) -> None:
    if not is_owner(message):
        return
    TRIGGER_BRIEF.touch()
    await message.answer("Brief generation triggered.")


@router.message(Command("weekly"))
async def cmd_weekly(message: Message) -> None:
    if not is_owner(message):
        return
    TRIGGER_WEEKLY.touch()
    await message.answer("Weekly review triggered.")


@router.message(Command("search"))
async def cmd_search(message: Message) -> None:
    if not is_owner(message):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Usage: /search <query>")
        return

    query = parts[1].strip()
    sm = get_sessionmaker()
    with sm() as session:
        stmt = (
            select(TgMessage.text, Chat.title)
            .join(Chat, Chat.chat_id == TgMessage.chat_id)
            .where(TgMessage.text.ilike(f"%{query}%"))
            .order_by(TgMessage.sent_at.desc())
            .limit(5)
        )
        rows = session.execute(stmt).all()

    if not rows:
        await message.answer("No messages found.")
        return

    lines = []
    for i, (msg_text, chat_title) in enumerate(rows, 1):
        snippet = (msg_text or "")[:120]
        lines.append(f"{i}. [{chat_title}] {snippet}")

    await message.answer("\n\n".join(lines))


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    if not is_owner(message):
        return
    PAUSE_FILE.touch()
    await message.answer("Ingestion paused.")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    if not is_owner(message):
        return
    with contextlib.suppress(FileNotFoundError):
        PAUSE_FILE.unlink()
    await message.answer("Ingestion resumed.")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not is_owner(message):
        return

    sm = get_sessionmaker()
    with sm() as session:
        total_messages = session.scalar(select(func.count()).select_from(TgMessage)) or 0
        total_understood = (
            session.scalar(select(func.count()).select_from(MessageUnderstanding)) or 0
        )
        unprocessed = total_messages - total_understood

        last_msg_ts = session.scalar(
            select(func.max(TgMessage.sent_at))
        )
        last_understanding_ts = session.scalar(
            select(func.max(MessageUnderstanding.processed_at))
        )

    paused = PAUSE_FILE.exists()
    pause_status = " (PAUSED)" if paused else ""

    lines = [
        f"Status{pause_status}",
        f"Brief LLM: {settings.llm_provider}",
        f"Total messages: {total_messages:,}",
        f"Understood: {total_understood:,}",
        f"Unprocessed: {unprocessed:,}",
        f"Last message: {last_msg_ts.isoformat() if last_msg_ts else 'N/A'}",
        f"Last understanding: {last_understanding_ts.isoformat() if last_understanding_ts else 'N/A'}",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not is_owner(message):
        return
    await message.answer(
        "Slash commands\n"
        "/help — show this help\n"
        "/start — onboarding\n"
        "/status — ingestion health + active brief LLM\n"
        "/brief — generate today's brief now\n"
        "/weekly — generate weekly review now\n"
        "/pause — pause ingestion\n"
        "/resume — resume ingestion\n"
        "/search <query> — keyword search over messages\n"
        "/ignore [ChatName] — mark a chat ignored\n"
        "/tag — re-run chat tagging\n"
        "/listtags — list active tags with AI guidance\n"
        "/newtag — create a new tag (FSM)\n"
        "/edittag — edit an existing tag (FSM)\n"
        "/feedback — give feedback on a brief item\n"
        "/done c<id> [note] — mark commitment done\n"
        "/cancel c<id> [reason] — cancel a commitment "
        "(or bare /cancel to abort an FSM flow)\n"
        "/reset — clear Claude conversation history\n"
        "\n"
        "Natural-language shortcuts (handled directly, no Claude call)\n"
        "Brief feedback:    \"#abcd useful\", \"#abcd not useful\", "
        "\"#abcd missed\", \"useful #abcd\"\n"
        "Commitment done:   \"done c42\", \"finished c42 sent today\", "
        "\"completed c42\"\n"
        "Commitment cancel: \"cancel c42\", \"drop c42\", \"forget c42\"\n"
        "Commitment Q&A:    \"explain c42\", \"what is c42\", "
        "\"tell me about c42 and c43\"\n"
        "Retag a chat:      \"#abcd prospect\" or \"prospect #abcd\" "
        "(any active /listtags tag)\n"
        "\n"
        "Anything else without a leading / goes to Claude.",
        parse_mode=None,
    )
