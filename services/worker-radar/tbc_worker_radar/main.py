"""worker-radar main entry point.

Runs a continuous poll loop every POLL_INTERVAL_SECONDS seconds,
calling the aggregator to process new signals into radar_alerts.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import structlog

from tbc_common.db.session import get_sessionmaker
from tbc_common.logging import configure_logging

from tbc_worker_radar.aggregator import run_aggregation

POLL_INTERVAL_SECONDS = 60

logger = structlog.get_logger(__name__)


def main() -> None:
    configure_logging("worker-radar")
    log = structlog.get_logger("worker_radar.main")
    log.info("starting", poll_interval=POLL_INTERVAL_SECONDS)

    Session = get_sessionmaker()
    # Start from epoch so we catch everything on first run
    last_checked_at: datetime = datetime(1970, 1, 1, tzinfo=timezone.utc)

    while True:
        try:
            with Session() as session:
                last_checked_at = run_aggregation(session, last_checked_at)
        except Exception:
            log.exception("aggregation_error")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
