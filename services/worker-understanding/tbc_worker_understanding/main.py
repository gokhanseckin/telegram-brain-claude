"""Entrypoint for the understanding worker.

Two modes (selected via `TBC_UNDERSTANDING_MODE`):

- ``brief-coupled`` (default): the heavy LLM understanding pass runs only on
  demand from the brief worker, signalled via ``/tmp/tbc_trigger_understanding``.
  A lightweight 5-second embed loop keeps `bge-m3` embeddings fresh between
  briefs so semantic_search/MCP stays usable.
- ``continuous``: the legacy 5-second poll loop that runs both embedding and
  LLM understanding for every batch. Kept reachable for fallback.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date
from pathlib import Path

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tbc_common.config import settings
from tbc_common.db.models import Message, MessageUnderstanding
from tbc_common.db.session import get_sessionmaker
from tbc_common.db.tags import get_active_tags
from tbc_common.db.understanding_queue import pending_understanding_rows
from tbc_common.logging import configure_logging
from tbc_common.prompts import MODEL_VERSION
from tbc_common.prompts.understanding import (
    build_understanding_system,
    build_understanding_system_batched,
)

from .ollama_client import OllamaClient
from .processor import process_message, process_message_batch

log = structlog.get_logger(__name__)

# A session_factory yields a transactional Session in a `with` block.
SessionFactory = Callable[[], AbstractContextManager[Session]]

UNDERSTANDING_TRIGGER_FILE = "/tmp/tbc_trigger_understanding"

# Embed loop cadence and SQL: pick messages in tagged chats from the last
# 3 days that have no message_understanding row (or one with embedding NULL),
# embed in bge-m3, write a partial row at model_version="embeddings-only-<date>".
# This is the same pattern bulk_embed.py uses for one-shot backfills.
EMBED_LOOP_INTERVAL_S = 5
EMBED_BATCH_SIZE = 64
EMBED_MAX_INPUT_CHARS = 8000

_EMBED_CANDIDATE_SQL = text("""
    SELECT m.chat_id, m.message_id, m.text
    FROM messages m
    LEFT JOIN message_understanding mu
      ON mu.chat_id = m.chat_id
     AND mu.message_id = m.message_id
    WHERE m.deleted_at IS NULL
      AND m.text IS NOT NULL
      AND m.text != ''
      AND m.sent_at >= NOW() - INTERVAL '3 days'
      AND (mu.chat_id IS NULL OR mu.embedding IS NULL)
      AND m.chat_id IN (
          SELECT chat_id FROM chats
          WHERE tag IS NOT NULL AND tag != 'ignore'
      )
    ORDER BY m.sent_at DESC
    LIMIT :batch_size
""")


def _poll(session: Session) -> list[tuple[int, int]]:
    """Pending LLM-understanding rows at the current MODEL_VERSION."""
    return pending_understanding_rows(session, model_version=MODEL_VERSION)  # type: ignore[no-any-return]


def _assemble_chat_aware_batch(
    msgs_all: list[Message],
    *,
    max_n: int,
    max_chats_per_batch: int,
    budget_chars: int,
) -> list[Message]:
    """Group candidates by chat, then either fill from one chat (if it has
    enough on its own) or round-robin from the top N chats. Each block of
    messages from one chat stays contiguous (oldest-first within chat) so
    Gemma sees coherent threads instead of an interleaved jumble.

    Returns the selected batch in chat-grouped, oldest-first order.
    """
    if not msgs_all:
        return []

    by_chat: dict[int, list[Message]] = defaultdict(list)
    for m in msgs_all:
        by_chat[m.chat_id].append(m)
    for cid in by_chat:
        by_chat[cid].sort(key=lambda m: m.sent_at)  # oldest-first per chat

    chats_sorted = sorted(by_chat.items(), key=lambda kv: len(kv[1]), reverse=True)

    # Case 1: top chat alone has >= max_n messages → fill batch from just that chat.
    if chats_sorted and len(chats_sorted[0][1]) >= max_n:
        return chats_sorted[0][1][:max_n]

    # Case 2: round-robin across the top max_chats_per_batch chats until max_n
    # reached or token budget exhausted.
    selected = chats_sorted[:max_chats_per_batch]
    iters = [iter(msgs) for _, msgs in selected]
    grouped: dict[int, list[Message]] = defaultdict(list)
    cum_chars = 0
    while iters and sum(len(g) for g in grouped.values()) < max_n:
        progress = False
        for it in iters[:]:
            try:
                m = next(it)
            except StopIteration:
                iters.remove(it)
                continue
            text_size = len(m.text or "") * 4
            if cum_chars + text_size > budget_chars:
                iters.remove(it)
                continue
            grouped[m.chat_id].append(m)
            cum_chars += text_size
            progress = True
            if sum(len(g) for g in grouped.values()) >= max_n:
                break
        if not progress:
            break

    # Emit chat-by-chat (preserves coherent thread blocks in the prompt).
    batch: list[Message] = []
    for cid, _ in selected:
        batch.extend(grouped.get(cid, []))
    return batch


def _read_batch_envs() -> tuple[int, int, int]:
    budget_chars = int(os.environ.get("TBC_UNDERSTANDING_BATCH_CHAR_BUDGET", "60000"))
    max_n = int(os.environ.get("TBC_UNDERSTANDING_BATCH_MAX_N", "20"))
    max_chats = int(os.environ.get("TBC_UNDERSTANDING_MAX_CHATS_PER_BATCH", "3"))
    return budget_chars, max_n, max_chats


async def _run_one_batch(
    *,
    session_factory: SessionFactory,
    ollama: OllamaClient,
    batched_prompt: str,
    understanding_prompt: str,
    use_batched: bool,
) -> int:
    """Drain one batch of pending LLM-understanding rows. Returns count
    processed (0 if the queue was empty)."""
    with session_factory() as session:
        pending = _poll(session)
    if not pending:
        return 0

    if use_batched:
        budget_chars, max_n, max_chats = _read_batch_envs()
        with session_factory() as session:
            msgs_all: list[Message] = []
            for chat_id, message_id in pending:
                m = session.get(Message, (chat_id, message_id))
                if m is not None:
                    msgs_all.append(m)

            msgs = _assemble_chat_aware_batch(
                msgs_all,
                max_n=max_n,
                max_chats_per_batch=max_chats,
                budget_chars=budget_chars,
            )
            if not msgs:
                return 0
            cum = sum(len(m.text or "") * 4 for m in msgs)
            chat_breakdown: dict[int, int] = {}
            for m in msgs:
                chat_breakdown[m.chat_id] = chat_breakdown.get(m.chat_id, 0) + 1
            log.info(
                "batch_assembled",
                n=len(msgs),
                approx_input_chars=cum,
                candidates=len(msgs_all),
                chats=chat_breakdown,
            )
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
                return 0
            return len(msgs)
    else:
        n_done = 0
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
                    n_done += 1
                except Exception:
                    log.exception(
                        "process_message_failed",
                        chat_id=chat_id,
                        message_id=message_id,
                    )
        return n_done


async def run_llm_bulk(
    *,
    session_factory: SessionFactory,
    ollama: OllamaClient,
    batched_prompt: str,
    understanding_prompt: str,
) -> int:
    """Drain the LLM-understanding queue until empty. Returns total messages
    processed across all batches in this run."""
    started = time.monotonic()
    use_batched = os.environ.get("TBC_UNDERSTANDING_BATCHED", "1") == "1"
    log.info("bulk_run_starting", model_version=MODEL_VERSION)
    total = 0
    while True:
        n = await _run_one_batch(
            session_factory=session_factory,
            ollama=ollama,
            batched_prompt=batched_prompt,
            understanding_prompt=understanding_prompt,
            use_batched=use_batched,
        )
        if n == 0:
            break
        total += n
    elapsed = round(time.monotonic() - started, 2)
    log.info("bulk_run_complete", processed=total, elapsed_seconds=elapsed)
    return total


async def run_loop_continuous(
    *,
    session_factory: SessionFactory,
    ollama: OllamaClient,
    batched_prompt: str,
    understanding_prompt: str,
) -> None:
    """Legacy 5-second poll loop. Identical behaviour to pre-refactor."""
    use_batched = os.environ.get("TBC_UNDERSTANDING_BATCHED", "1") == "1"
    while True:
        n = await _run_one_batch(
            session_factory=session_factory,
            ollama=ollama,
            batched_prompt=batched_prompt,
            understanding_prompt=understanding_prompt,
            use_batched=use_batched,
        )
        if n == 0:
            log.debug("queue_empty", sleep_seconds=5)
            await asyncio.sleep(5)


# --- Real-time embedding loop (brief-coupled mode) -----------------------------


def _fetch_embed_candidates(session: Session, batch_size: int) -> list[tuple[int, int, str]]:
    rows = session.execute(_EMBED_CANDIDATE_SQL, {"batch_size": batch_size}).fetchall()
    return [(r.chat_id, r.message_id, r.text) for r in rows]


def _upsert_embeddings_only(
    session: Session,
    rows: list[tuple[int, int]],
    embeddings: list[list[float]],
    model_version: str,
) -> None:
    values = [
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "model_version": model_version,
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


async def embed_loop(*, session_factory: SessionFactory, ollama: OllamaClient) -> None:
    """Continuously keep `bge-m3` embeddings fresh for new messages so
    semantic_search stays current between LLM-understanding runs.

    Writes partial rows at ``model_version="embeddings-only-<YYYY-MM-DD>"``;
    the LLM bulk pass later writes the canonical row at the LLM
    ``MODEL_VERSION`` (separate row, both coexist).
    """
    log.info("embed_loop_starting", embedding_model=settings.embedding_model)
    while True:
        with session_factory() as session:
            batch = _fetch_embed_candidates(session, EMBED_BATCH_SIZE)
        if not batch:
            await asyncio.sleep(EMBED_LOOP_INTERVAL_S)
            continue

        inputs = [(t or "")[:EMBED_MAX_INPUT_CHARS] for _, _, t in batch]
        ids = [(chat_id, message_id) for chat_id, message_id, _ in batch]
        model_version = f"embeddings-only-{date.today().isoformat()}"

        embeddings = await _embed_with_retry(ollama, settings.embedding_model, inputs)
        if embeddings is None:
            await asyncio.sleep(EMBED_LOOP_INTERVAL_S)
            continue
        if len(embeddings) != len(batch):
            log.error(
                "embedding_count_mismatch",
                expected=len(batch),
                got=len(embeddings),
            )
            await asyncio.sleep(EMBED_LOOP_INTERVAL_S)
            continue

        with session_factory() as session:
            _upsert_embeddings_only(session, ids, embeddings, model_version)
        log.info("embed_loop_batch", count=len(batch), model_version=model_version)


# --- Trigger-file watcher (brief-coupled mode) ---------------------------------


TRIGGER_WATCH_INTERVAL_S = 30


async def trigger_watcher(
    *,
    session_factory: SessionFactory,
    ollama: OllamaClient,
    batched_prompt: str,
    understanding_prompt: str,
) -> None:
    """Watch ``/tmp/tbc_trigger_understanding``; on each appearance, drain the
    LLM-understanding queue and delete the file. Idempotent — extra touches
    are harmless because the drain queries the DB itself."""
    log.info("trigger_watcher_starting", path=UNDERSTANDING_TRIGGER_FILE)
    trigger_path = Path(UNDERSTANDING_TRIGGER_FILE)
    while True:
        try:
            if trigger_path.exists():
                log.info("trigger_file_detected", path=UNDERSTANDING_TRIGGER_FILE)
                try:
                    trigger_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as e:
                    log.error(
                        "trigger_file_remove_failed",
                        path=UNDERSTANDING_TRIGGER_FILE,
                        error=str(e),
                    )
                try:
                    await run_llm_bulk(
                        session_factory=session_factory,
                        ollama=ollama,
                        batched_prompt=batched_prompt,
                        understanding_prompt=understanding_prompt,
                    )
                except Exception:
                    log.exception("bulk_run_failed_from_trigger")
        except Exception:
            log.exception("trigger_watcher_iteration_failed")
        await asyncio.sleep(TRIGGER_WATCH_INTERVAL_S)


# --- Entrypoints --------------------------------------------------------------


async def _run_brief_coupled() -> None:
    configure_logging("worker-understanding")
    log.info("starting", model_version=MODEL_VERSION, mode="brief-coupled")

    session_factory = get_sessionmaker()
    ollama = OllamaClient(settings.ollama_base_url)

    with session_factory() as session:
        tags = get_active_tags(session)
    understanding_prompt = build_understanding_system(tags)
    batched_prompt = build_understanding_system_batched(tags)

    await asyncio.gather(
        embed_loop(session_factory=session_factory, ollama=ollama),
        trigger_watcher(
            session_factory=session_factory,
            ollama=ollama,
            batched_prompt=batched_prompt,
            understanding_prompt=understanding_prompt,
        ),
    )


async def _run_continuous() -> None:
    configure_logging("worker-understanding")
    log.info("starting", model_version=MODEL_VERSION, mode="continuous")

    session_factory = get_sessionmaker()
    ollama = OllamaClient(settings.ollama_base_url)

    with session_factory() as session:
        tags = get_active_tags(session)
    understanding_prompt = build_understanding_system(tags)
    batched_prompt = build_understanding_system_batched(tags)

    await run_loop_continuous(
        session_factory=session_factory,
        ollama=ollama,
        batched_prompt=batched_prompt,
        understanding_prompt=understanding_prompt,
    )


# Backwards-compatible alias — older entrypoints / tests may import run_loop.
async def run_loop() -> None:
    await _run_continuous()


def main() -> None:
    mode = settings.understanding_mode
    if mode == "continuous":
        asyncio.run(_run_continuous())
    elif mode == "brief-coupled":
        asyncio.run(_run_brief_coupled())
    else:
        raise RuntimeError(
            f"TBC_UNDERSTANDING_MODE must be 'brief-coupled' or 'continuous', got: {mode!r}"
        )


if __name__ == "__main__":
    main()
