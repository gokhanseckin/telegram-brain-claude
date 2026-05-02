"""Poll-loop entrypoint for the understanding worker."""

from __future__ import annotations

import asyncio
from collections import defaultdict

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session
from tbc_common.config import settings
from tbc_common.db.models import Message
from tbc_common.db.session import get_sessionmaker
from tbc_common.db.tags import get_active_tags
from tbc_common.logging import configure_logging
from tbc_common.prompts import MODEL_VERSION
from tbc_common.prompts.understanding import (
    build_understanding_system,
    build_understanding_system_batched,
)

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
    LIMIT 200
""")


def _poll(session: Session) -> list[tuple[int, int]]:
    rows = session.execute(_POLL_SQL, {"model_version": MODEL_VERSION}).fetchall()
    return [(r.chat_id, r.message_id) for r in rows]


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

                # Chat-aware batching:
                #   - if a chat has >= max_n pending, fill the batch from JUST that chat
                #     (one coherent thread, ideal for Gemma + auto-resolve detection)
                #   - otherwise, round-robin across the top `max_chats_per_batch` chats,
                #     with each chat's messages emitted as a contiguous block (still
                #     keeps each thread coherent inside the prompt)
                #   - token budget = char heuristic (~3 chars/token mixed TR/EN, 4x for
                #     prior-context overhead). Default 60K chars ≈ 20K tokens, well below
                #     Gemma 4's 256K context.
                budget_chars = int(_os.environ.get("TBC_UNDERSTANDING_BATCH_CHAR_BUDGET", "60000"))
                max_n = int(_os.environ.get("TBC_UNDERSTANDING_BATCH_MAX_N", "20"))
                max_chats_per_batch = int(_os.environ.get("TBC_UNDERSTANDING_MAX_CHATS_PER_BATCH", "3"))

                msgs = _assemble_chat_aware_batch(
                    msgs_all,
                    max_n=max_n,
                    max_chats_per_batch=max_chats_per_batch,
                    budget_chars=budget_chars,
                )
                cum = sum(len(m.text or "") * 4 for m in msgs)
                if msgs:
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
