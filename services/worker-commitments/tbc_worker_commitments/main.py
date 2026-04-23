"""worker-commitments main entry point.

Runs three jobs in a single process on separate schedules:
- Commitment extraction: every 30 seconds
- Relationship state recomputation: every 10 minutes
- Stale commitment detection: every hour
"""

from __future__ import annotations

import time

import structlog
from tbc_common.db.session import get_sessionmaker
from tbc_common.logging import configure_logging

from tbc_worker_commitments.extractor import extract_commitments
from tbc_worker_commitments.relationship import recompute_relationship_states
from tbc_worker_commitments.stale import mark_stale_commitments

EXTRACTION_INTERVAL_S = 30
RELATIONSHIP_INTERVAL_S = 600  # 10 minutes
STALE_INTERVAL_S = 3600  # 1 hour

logger = structlog.get_logger(__name__)


def main() -> None:
    configure_logging("worker-commitments")
    log = structlog.get_logger("worker_commitments.main")
    log.info(
        "starting",
        extraction_interval=EXTRACTION_INTERVAL_S,
        relationship_interval=RELATIONSHIP_INTERVAL_S,
        stale_interval=STALE_INTERVAL_S,
    )

    session_factory = get_sessionmaker()

    last_relationship_run: float = 0.0
    last_stale_run: float = 0.0

    while True:
        now = time.monotonic()

        # Job 1: Commitment extraction (every 30s)
        try:
            with session_factory() as session:
                created = extract_commitments(session)
                if created:
                    log.info("extraction_done", created=created)
        except Exception:
            log.exception("extraction_error")

        # Job 2: Relationship state (every 10 min)
        if now - last_relationship_run >= RELATIONSHIP_INTERVAL_S:
            try:
                with session_factory() as session:
                    updated = recompute_relationship_states(session)
                    log.info("relationship_recompute_done", updated=updated)
            except Exception:
                log.exception("relationship_error")
            last_relationship_run = now

        # Job 3: Stale detection (every hour)
        if now - last_stale_run >= STALE_INTERVAL_S:
            try:
                with session_factory() as session:
                    staled = mark_stale_commitments(session)
                    log.info("stale_detection_done", staled=staled)
            except Exception:
                log.exception("stale_error")
            last_stale_run = now

        time.sleep(EXTRACTION_INTERVAL_S)


if __name__ == "__main__":
    main()
