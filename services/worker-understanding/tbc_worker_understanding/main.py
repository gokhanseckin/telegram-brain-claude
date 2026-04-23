"""Poll-loop entrypoint for the understanding worker."""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session
from tbc_common.config import settings
from tbc_common.db.models import Message
from tbc_common.db.session import get_sessionmaker
from tbc_common.logging import configure_logging
from tbc_common.prompts import MODEL_VERSION

from .ollama_client import OllamaClient
from .processor import process_message

log = structlog.get_logger(__name__)

_POLL_SQL = text("""
    SELECT m.chat_id, m.message_id
    FROM messages m
    LEFT JOIN message_understanding mu
      ON mu.chat_id = m.chat_id
     AND mu.message_id = m.message_id
     AND mu.model_version = :model_version
    WHERE mu.message_id IS NULL
      AND m.deleted_at IS NULL
      AND m.text IS NOT NULL
      AND m.text != ''
      AND m.chat_id IN (
          SELECT chat_id FROM chats
          WHERE tag IS NOT NULL AND tag != 'ignore'
      )
    ORDER BY m.sent_at ASC
    LIMIT 10
""")


def _poll(session: Session) -> list[tuple[int, int]]:
    rows = session.execute(_POLL_SQL, {"model_version": MODEL_VERSION}).fetchall()
    return [(r.chat_id, r.message_id) for r in rows]


async def run_loop() -> None:
    configure_logging("worker-understanding")
    log.info("starting", model_version=MODEL_VERSION)

    Session = get_sessionmaker()
    ollama = OllamaClient(settings.ollama_base_url)

    while True:
        with Session() as session:
            pending = _poll(session)

        if not pending:
            log.debug("queue_empty", sleep_seconds=5)
            await asyncio.sleep(5)
            continue

        for chat_id, message_id in pending:
            with Session() as session:
                message = session.get(Message, (chat_id, message_id))
                if message is None:
                    continue
                try:
                    await process_message(
                        message=message,
                        session=session,
                        ollama=ollama,
                        understanding_model=settings.understanding_model,
                        embedding_model=settings.embedding_model,
                    )
                except Exception:
                    log.exception(
                        "process_message_failed",
                        chat_id=chat_id,
                        message_id=message_id,
                    )


def main() -> None:
    asyncio.run(run_loop())


if __name__ == "__main__":
    main()
