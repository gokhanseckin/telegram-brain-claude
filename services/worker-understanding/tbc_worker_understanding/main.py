"""Poll-loop entrypoint for the understanding worker."""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session
from tbc_common.config import settings
from tbc_common.db.models import Message
from tbc_common.db.session import get_sessionmaker
from tbc_common.db.tags import get_active_tags
from tbc_common.logging import configure_logging
from tbc_common.prompts import MODEL_VERSION
from tbc_common.prompts.understanding import build_understanding_system, build_understanding_system_batched

from .ollama_client import OllamaClient
from .processor import process_message, process_message_batch

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
      AND m.sent_at >= NOW() - INTERVAL '3 days'
      AND m.chat_id IN (
          SELECT chat_id FROM chats
          WHERE tag IS NOT NULL AND tag != 'ignore'
      )
    ORDER BY m.sent_at DESC
    LIMIT 100
""")


def _poll(session: Session) -> list[tuple[int, int]]:
    rows = session.execute(_POLL_SQL, {"model_version": MODEL_VERSION}).fetchall()
    return [(r.chat_id, r.message_id) for r in rows]


async def run_loop() -> None:
    configure_logging("worker-understanding")
    log.info("starting", model_version=MODEL_VERSION)

    session_factory = get_sessionmaker()
    ollama = OllamaClient(settings.ollama_base_url)

    with session_factory() as session:
        tags = get_active_tags(session)
    understanding_prompt = build_understanding_system(tags)
    batched_prompt = build_understanding_system_batched(tags)

    import os as _os
    use_batched = _os.environ.get("TBC_UNDERSTANDING_BATCHED", "1") == "1"

    while True:
        with session_factory() as session:
            pending = _poll(session)

        if not pending:
            log.debug("queue_empty", sleep_seconds=5)
            await asyncio.sleep(5)
            continue

        if use_batched:
            with session_factory() as session:
                msgs_all = []
                for chat_id, message_id in pending:
                    m = session.get(Message, (chat_id, message_id))
                    if m is not None:
                        msgs_all.append(m)

                # Token-aware batching: heuristic 3 chars/token (mixed Turkish/English).
                # Each target message gets ~3x its size in prior context, so multiplier 4.
                # Budget headroom: model 128K context - 12K output - 3K system prompt = ~110K input.
                # Use 30K conservative budget per batch.
                budget_chars = int(_os.environ.get("TBC_UNDERSTANDING_BATCH_CHAR_BUDGET", "90000"))
                max_n = int(_os.environ.get("TBC_UNDERSTANDING_BATCH_MAX_N", "50"))
                msgs = []
                cum = 0
                for m in msgs_all:
                    text_size = len(m.text or "") * 4  # approx with prior context
                    if msgs and (cum + text_size > budget_chars or len(msgs) >= max_n):
                        break
                    msgs.append(m)
                    cum += text_size

                msgs.reverse()  # oldest-first inside batch for natural flow + in-batch auto-resolve
                if msgs:
                    log.info("batch_assembled", n=len(msgs), approx_input_chars=cum, candidates=len(msgs_all))
                    try:
                        await process_message_batch(
                            messages=msgs,
                            session=session,
                            ollama=ollama,
                            understanding_model=settings.understanding_model,
                            embedding_model=settings.embedding_model,
                            system_prompt_batched=batched_prompt,
                        )
                    except Exception:
                        log.exception("process_message_batch_failed", n=len(msgs))
        else:
            for chat_id, message_id in pending:
                with session_factory() as session:
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
                            system_prompt=understanding_prompt,
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
