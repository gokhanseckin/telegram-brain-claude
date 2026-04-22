"""File-based IPC for pause/resume of live ingestion.

The Telegram bot service (`services/tg-bot`) implements the `/pause` and
`/resume` commands. When the user sends `/pause`, the bot creates the file
`/tmp/tbc_pause`. When the user sends `/resume`, the bot removes it.

This module provides:
- `is_paused()` — synchronous predicate used inside event handlers.
- `start_pause_monitor(interval)` — background asyncio task that polls the
  file and logs state transitions. Purely informational; handlers call
  `is_paused()` directly on every event.

See docs/mvp-spec.md §3 (pause/resume IPC).
"""

from __future__ import annotations

import asyncio
import os

import structlog

log = structlog.get_logger(__name__)

PAUSE_FILE = "/tmp/tbc_pause"

_was_paused: bool = False


def is_paused() -> bool:
    """Return True if the pause file exists (ingestion should be suppressed)."""
    return os.path.exists(PAUSE_FILE)


async def start_pause_monitor(interval: float = 5.0) -> None:
    """Poll the pause file every `interval` seconds and log state changes.

    This coroutine runs indefinitely. Start it as an asyncio background task:

        asyncio.create_task(start_pause_monitor())
    """
    global _was_paused
    log.info("pause_monitor_started", interval_seconds=interval, pause_file=PAUSE_FILE)
    while True:
        paused = is_paused()
        if paused and not _was_paused:
            log.info("ingestion_paused", pause_file=PAUSE_FILE)
        elif not paused and _was_paused:
            log.info("ingestion_resumed", pause_file=PAUSE_FILE)
        _was_paused = paused
        await asyncio.sleep(interval)
