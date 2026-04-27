"""Entry point for the chat auto-tagger worker.

Periodically classifies untagged, unlocked chats. Also responds to a manual
trigger file at /tmp/tbc_trigger_tagger.
"""

from __future__ import annotations

import os
import time

import structlog
from tbc_common.config import settings
from tbc_common.db.session import get_sessionmaker
from tbc_common.logging import configure_logging

from tbc_worker_chat_tagger.classifier import run_once

log = structlog.get_logger(__name__)

TRIGGER_FILE = "/tmp/tbc_trigger_tagger"


def _check_trigger() -> bool:
    if not os.path.exists(TRIGGER_FILE):
        return False
    try:
        os.remove(TRIGGER_FILE)
    except OSError:
        log.exception("trigger_remove_failed")
        return False
    return True


def main() -> None:
    configure_logging("worker-chat-tagger")
    log.info("worker_chat_tagger_starting", interval=settings.tagger_interval_seconds)

    session_factory = get_sessionmaker()
    last_run: float = 0.0

    while True:
        now = time.monotonic()
        triggered = _check_trigger()

        due = (now - last_run) >= settings.tagger_interval_seconds

        if triggered or due or last_run == 0.0:
            try:
                with session_factory() as session:
                    run_once(session)
            except Exception:
                log.exception("tagger_run_failed")
            last_run = now

        time.sleep(30)


if __name__ == "__main__":
    main()
