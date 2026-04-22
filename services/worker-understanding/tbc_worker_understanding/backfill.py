"""CLI command to reprocess all messages for a specific chat.

Usage:
    python -m tbc_worker_understanding.backfill --chat-id <id>
"""

from __future__ import annotations

import argparse
import asyncio

import structlog
from sqlalchemy import select

from tbc_common.config import settings
from tbc_common.db.models import Message
from tbc_common.db.session import get_sessionmaker
from tbc_common.logging import configure_logging

from .ollama_client import OllamaClient
from .processor import process_message

log = structlog.get_logger(__name__)


async def run_backfill(chat_id: int) -> None:
    configure_logging("worker-understanding-backfill")
    log.info("backfill_start", chat_id=chat_id)

    SessionFactory = get_sessionmaker()
    ollama = OllamaClient(settings.ollama_base_url)

    with SessionFactory() as session:
        messages = (
            session.execute(
                select(Message)
                .where(
                    Message.chat_id == chat_id,
                    Message.deleted_at.is_(None),
                    Message.text.isnot(None),
                    Message.text != "",
                )
                .order_by(Message.sent_at.asc())
            )
            .scalars()
            .all()
        )

    log.info("backfill_messages_found", chat_id=chat_id, count=len(messages))

    for message in messages:
        with SessionFactory() as session:
            # Re-fetch inside fresh session to avoid detached state
            msg = session.get(Message, (message.chat_id, message.message_id))
            if msg is None:
                continue
            try:
                await process_message(
                    message=msg,
                    session=session,
                    ollama=ollama,
                    understanding_model=settings.understanding_model,
                    embedding_model=settings.embedding_model,
                )
            except Exception:
                log.exception(
                    "backfill_message_failed",
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                )

    log.info("backfill_complete", chat_id=chat_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill understanding for a chat.")
    parser.add_argument("--chat-id", type=int, required=True, help="Telegram chat_id to reprocess")
    args = parser.parse_args()
    asyncio.run(run_backfill(args.chat_id))


if __name__ == "__main__":
    main()
