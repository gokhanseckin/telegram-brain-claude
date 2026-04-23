"""One-shot CLI to bulk-generate bge-m3 embeddings for tagged chats.

Unlocks semantic_search / RAG Q&A without waiting on the Qwen understanding
pass. Upserts partial rows into message_understanding with embedding +
model_version="embeddings-only-<date>", leaving Qwen-derived fields NULL. The
regular worker-understanding poll loop later overwrites these with full
understanding (its LEFT JOIN filters on the current MODEL_VERSION, so these
rows still look pending to it).

Usage:
    python -m tbc_worker_understanding.bulk_embed [--batch-size 128] [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import date

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tbc_common.config import settings
from tbc_common.db.models import MessageUnderstanding
from tbc_common.db.session import get_sessionmaker
from tbc_common.logging import configure_logging

from .ollama_client import OllamaClient

log = structlog.get_logger(__name__)

MODEL_VERSION = f"embeddings-only-{date.today().isoformat()}"

MAX_INPUT_CHARS = 8000

_CANDIDATE_SQL = text("""
    SELECT m.chat_id, m.message_id, m.text
    FROM messages m
    LEFT JOIN message_understanding mu
      ON mu.chat_id = m.chat_id
     AND mu.message_id = m.message_id
    WHERE m.deleted_at IS NULL
      AND m.text IS NOT NULL
      AND m.text != ''
      AND (mu.chat_id IS NULL OR mu.embedding IS NULL)
      AND m.chat_id IN (
          SELECT chat_id FROM chats
          WHERE tag IS NOT NULL AND tag != 'ignore'
      )
    ORDER BY m.sent_at ASC
    LIMIT :batch_size
""")

_COUNT_SQL = text("""
    SELECT COUNT(*)
    FROM messages m
    LEFT JOIN message_understanding mu
      ON mu.chat_id = m.chat_id
     AND mu.message_id = m.message_id
    WHERE m.deleted_at IS NULL
      AND m.text IS NOT NULL
      AND m.text != ''
      AND (mu.chat_id IS NULL OR mu.embedding IS NULL)
      AND m.chat_id IN (
          SELECT chat_id FROM chats
          WHERE tag IS NOT NULL AND tag != 'ignore'
      )
""")


def _fetch_batch(session: Session, batch_size: int) -> list[tuple[int, int, str]]:
    rows = session.execute(_CANDIDATE_SQL, {"batch_size": batch_size}).fetchall()
    return [(r.chat_id, r.message_id, r.text) for r in rows]


async def _embed_with_retry(
    ollama: OllamaClient, model: str, inputs: list[str]
) -> list[list[float]] | None:
    try:
        return await ollama.embed_batch(model=model, inputs=inputs)
    except httpx.HTTPError as exc:
        log.warning("embed_batch_failed_retrying", error=str(exc))
        await asyncio.sleep(2)
        try:
            return await ollama.embed_batch(model=model, inputs=inputs)
        except httpx.HTTPError as exc2:
            log.error("embed_batch_failed_giving_up", error=str(exc2))
            return None


def _upsert_embeddings(
    session: Session,
    rows: list[tuple[int, int]],
    embeddings: list[list[float]],
) -> None:
    values = [
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "model_version": MODEL_VERSION,
            "embedding": embedding,
        }
        for (chat_id, message_id), embedding in zip(rows, embeddings, strict=True)
    ]
    stmt = pg_insert(MessageUnderstanding).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["chat_id", "message_id"],
        set_={
            "model_version": stmt.excluded.model_version,
            "embedding": stmt.excluded.embedding,
            "processed_at": text("now()"),
        },
    )
    session.execute(stmt)
    session.commit()


async def run_bulk_embed(batch_size: int, limit: int | None, dry_run: bool) -> None:
    configure_logging("worker-understanding-bulk-embed")

    SessionFactory = get_sessionmaker()

    with SessionFactory() as session:
        total_pending = session.execute(_COUNT_SQL).scalar_one()

    log.info(
        "bulk_embed_start",
        total_pending=total_pending,
        batch_size=batch_size,
        limit=limit,
        model_version=MODEL_VERSION,
        embedding_model=settings.embedding_model,
        dry_run=dry_run,
    )

    if dry_run:
        return

    ollama = OllamaClient(settings.ollama_base_url)
    processed = 0
    started = time.monotonic()

    while True:
        remaining = None if limit is None else max(limit - processed, 0)
        if remaining == 0:
            break
        this_batch = batch_size if remaining is None else min(batch_size, remaining)

        with SessionFactory() as session:
            batch = _fetch_batch(session, this_batch)

        if not batch:
            break

        inputs = [(t or "")[:MAX_INPUT_CHARS] for _, _, t in batch]
        ids = [(chat_id, message_id) for chat_id, message_id, _ in batch]

        batch_started = time.monotonic()
        embeddings = await _embed_with_retry(ollama, settings.embedding_model, inputs)
        if embeddings is None:
            log.warning("skipping_batch_after_retry", size=len(batch))
            continue

        if len(embeddings) != len(batch):
            log.error(
                "embedding_count_mismatch",
                expected=len(batch),
                got=len(embeddings),
            )
            continue

        with SessionFactory() as session:
            _upsert_embeddings(session, ids, embeddings)

        processed += len(batch)
        batch_elapsed = time.monotonic() - batch_started
        total_elapsed = time.monotonic() - started
        rate = processed / total_elapsed if total_elapsed > 0 else 0.0
        log.info(
            "batch_embedded",
            count=len(batch),
            processed=processed,
            total_pending=total_pending,
            batch_seconds=round(batch_elapsed, 2),
            rate_per_sec=round(rate, 2),
        )

    total_elapsed = time.monotonic() - started
    log.info(
        "bulk_embed_complete",
        processed=processed,
        elapsed_seconds=round(total_elapsed, 2),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk-generate bge-m3 embeddings for tagged-chat messages missing an embedding.",
    )
    parser.add_argument("--batch-size", type=int, default=128, help="Messages per Ollama request.")
    parser.add_argument("--limit", type=int, default=None, help="Optional cap for testing.")
    parser.add_argument("--dry-run", action="store_true", help="Count candidates and exit.")
    args = parser.parse_args()
    asyncio.run(run_bulk_embed(args.batch_size, args.limit, args.dry_run))


if __name__ == "__main__":
    main()
