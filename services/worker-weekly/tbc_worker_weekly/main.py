"""Scheduler and trigger file poll for the Weekly Review worker."""

from __future__ import annotations

import os
import time
from datetime import date

import structlog
from apscheduler.schedulers.background import BackgroundScheduler

from tbc_common.config import settings
from tbc_common.db.session import get_sessionmaker
from tbc_common.logging import configure_logging

from tbc_worker_weekly.assembler import build_weekly_input, monday_of_week
from tbc_worker_weekly.sender import call_batch_api, post_to_telegram, save_weekly

log = structlog.get_logger(__name__)

TRIGGER_FILE = "/tmp/tbc_trigger_weekly"

# APScheduler day-of-week mapping
DAY_MAP = {
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    "saturday": "sat",
    "sunday": "sun",
}


def run_weekly() -> None:
    """Assemble weekly input, call Batch API, deliver, persist results."""
    log.info("weekly_run_starting")
    Session = get_sessionmaker()
    today = date.today()
    monday = monday_of_week(today)

    with Session() as session:
        weekly_input = build_weekly_input(session)
        weekly_text = call_batch_api(weekly_input, today)
        log.info("weekly_generated", length=len(weekly_text))

        post_to_telegram(weekly_text)
        save_weekly(session, weekly_text, monday)

    log.info("weekly_run_complete", monday=monday.isoformat())


def check_trigger_file() -> None:
    """If /tmp/tbc_trigger_weekly exists, run weekly immediately and delete the file."""
    if os.path.exists(TRIGGER_FILE):
        log.info("trigger_file_detected", path=TRIGGER_FILE)
        try:
            os.remove(TRIGGER_FILE)
        except OSError:
            pass
        try:
            run_weekly()
        except Exception:
            log.exception("weekly_run_failed_from_trigger")


def main() -> None:
    configure_logging("worker-weekly")
    log.info("worker_weekly_starting")

    hour = int(settings.weekly_time.split(":")[0])
    minute = int(settings.weekly_time.split(":")[1])
    day_of_week = DAY_MAP.get(settings.weekly_day.lower(), "mon")

    scheduler = BackgroundScheduler(timezone=settings.brief_tz)
    scheduler.add_job(
        run_weekly,
        "cron",
        day_of_week=day_of_week,
        hour=hour,
        minute=minute,
        id="weekly_review",
    )
    scheduler.start()
    log.info(
        "scheduler_started",
        day=settings.weekly_day,
        time=settings.weekly_time,
        tz=settings.brief_tz,
    )

    try:
        while True:
            check_trigger_file()
            time.sleep(30)
    except (KeyboardInterrupt, SystemExit):
        log.info("worker_weekly_stopping")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
