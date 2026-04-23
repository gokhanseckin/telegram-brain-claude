"""Scheduler and trigger file poll for the Morning Brief worker."""

from __future__ import annotations

import contextlib
import os
import time
from datetime import date

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from tbc_common.config import settings
from tbc_common.db.session import get_sessionmaker
from tbc_common.logging import configure_logging

from tbc_worker_brief.assembler import build_cached_context, build_fresh_input
from tbc_worker_brief.sender import call_anthropic, post_to_telegram, save_brief, stamp_radar_alerts

log = structlog.get_logger(__name__)

TRIGGER_FILE = "/tmp/tbc_trigger_brief"


def run_brief() -> None:
    """Assemble inputs, call Anthropic, deliver brief, persist results."""
    log.info("brief_run_starting")
    session_factory = get_sessionmaker()
    today = date.today()

    with session_factory() as session:
        cached_context = build_cached_context(session)
        fresh_input, alert_ids = build_fresh_input(session)

        brief_text = call_anthropic(cached_context, fresh_input)
        log.info("brief_generated", length=len(brief_text))

        post_to_telegram(brief_text)
        save_brief(session, brief_text, today)
        stamp_radar_alerts(session, alert_ids)

    log.info("brief_run_complete", date=today.isoformat())


def check_trigger_file() -> None:
    """If /tmp/tbc_trigger_brief exists, run brief immediately and delete the file."""
    if os.path.exists(TRIGGER_FILE):
        log.info("trigger_file_detected", path=TRIGGER_FILE)
        with contextlib.suppress(OSError):
            os.remove(TRIGGER_FILE)
        try:
            run_brief()
        except Exception:
            log.exception("brief_run_failed_from_trigger")


def main() -> None:
    configure_logging("worker-brief")
    log.info("worker_brief_starting")

    hour, minute = int(settings.brief_time.split(":")[0]), int(settings.brief_time.split(":")[1])

    scheduler = BackgroundScheduler(timezone=settings.brief_tz)
    scheduler.add_job(
        run_brief,
        "cron",
        hour=hour,
        minute=minute,
        id="daily_brief",
    )
    scheduler.start()
    log.info("scheduler_started", time=settings.brief_time, tz=settings.brief_tz)

    try:
        while True:
            check_trigger_file()
            time.sleep(30)
    except (KeyboardInterrupt, SystemExit):
        log.info("worker_brief_stopping")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
