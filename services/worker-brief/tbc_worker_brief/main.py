"""Scheduler and trigger file poll for the Morning Brief worker."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date
from pathlib import Path

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session
from tbc_common.config import settings
from tbc_common.db.session import get_sessionmaker
from tbc_common.db.tags import get_active_tags
from tbc_common.db.understanding_queue import pending_understanding_count
from tbc_common.logging import configure_logging
from tbc_common.prompts import MODEL_VERSION
from tbc_common.prompts.brief import build_brief_system

from tbc_worker_brief.assembler import build_cached_context, build_fresh_input
from tbc_worker_brief.sender import call_llm, post_to_telegram, save_brief, stamp_radar_alerts

log = structlog.get_logger(__name__)

# A session_factory yields a transactional Session in a `with` block.
SessionFactory = Callable[[], AbstractContextManager[Session]]

TRIGGER_FILE = "/tmp/tbc_trigger_brief"
UNDERSTANDING_TRIGGER_FILE = "/tmp/tbc_trigger_understanding"


def _ensure_understanding_caught_up(session_factory: SessionFactory) -> None:
    """Block until the LLM-understanding queue is empty (or until the
    timeout fires).

    On entry, count pending messages at the current LLM `MODEL_VERSION`.
    If non-zero, touch ``/tmp/tbc_trigger_understanding`` to wake the
    understanding worker and poll the count every 5 seconds, logging
    progress every 30 seconds. On timeout (default 300s) log a warning
    and return — the brief proceeds with whatever's already understood
    rather than hanging the user.
    """
    with session_factory() as session:
        pending = pending_understanding_count(session, model_version=MODEL_VERSION)
    if pending == 0:
        return

    log.info("pending_understanding_detected", pending=pending, model_version=MODEL_VERSION)
    try:
        Path(UNDERSTANDING_TRIGGER_FILE).touch()
    except OSError as e:
        log.error("understanding_trigger_touch_failed", error=str(e))
        # Fall through — worker may still be running its own loop.

    timeout = settings.brief_pre_understanding_timeout_s
    started = time.monotonic()
    last_progress_log = 0.0
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= timeout:
            log.warning(
                "understanding_drain_timeout",
                pending=pending,
                timeout_s=timeout,
            )
            return
        time.sleep(5)
        with session_factory() as session:
            pending = pending_understanding_count(session, model_version=MODEL_VERSION)
        if pending == 0:
            log.info("understanding_drained", elapsed_s=int(time.monotonic() - started))
            return
        if time.monotonic() - last_progress_log >= 30:
            log.info(
                "understanding_drain_progress",
                pending=pending,
                elapsed_s=int(time.monotonic() - started),
            )
            last_progress_log = time.monotonic()


def run_brief() -> None:
    """Assemble inputs, call Anthropic, deliver brief, persist results."""
    log.info("brief_run_starting")
    session_factory = get_sessionmaker()
    today = date.today()

    with session_factory() as session:
        tags = get_active_tags(session)
        system_prompt = build_brief_system(tags)
        cached_context = build_cached_context(session)
        fresh_input, alert_ids = build_fresh_input(session)

        brief_text = call_llm(cached_context, fresh_input, system_prompt=system_prompt)
        log.info("brief_generated", length=len(brief_text))

        post_to_telegram(brief_text)
        save_brief(session, brief_text, today)
        stamp_radar_alerts(session, alert_ids)

    log.info("brief_run_complete", date=today.isoformat())


def run_brief_with_drain() -> None:
    """Drain the LLM-understanding queue first, then generate the brief.

    Used by both the 07:00 cron and the on-demand /brief trigger so every
    brief reflects the latest messages.
    """
    session_factory = get_sessionmaker()
    _ensure_understanding_caught_up(session_factory)
    run_brief()


def check_trigger_file() -> None:
    """If /tmp/tbc_trigger_brief exists, run brief immediately and delete the file."""
    if os.path.exists(TRIGGER_FILE):
        log.info("trigger_file_detected", path=TRIGGER_FILE)
        try:
            os.remove(TRIGGER_FILE)
        except OSError as e:
            log.error("trigger_file_remove_failed", path=TRIGGER_FILE, error=str(e))
            return
        try:
            run_brief_with_drain()
        except Exception:
            log.exception("brief_run_failed_from_trigger")


def main() -> None:
    configure_logging("worker-brief")
    log.info("worker_brief_starting")

    if settings.llm_provider == "anthropic" and settings.anthropic_api_key is None:
        raise RuntimeError("ANTHROPIC_API_KEY is required when TBC_LLM_PROVIDER=anthropic")
    if settings.llm_provider == "deepseek" and settings.deepseek_api_key is None:
        raise RuntimeError("DEEPSEEK_API_KEY is required when TBC_LLM_PROVIDER=deepseek")

    parts = settings.brief_time.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise RuntimeError(f"TBC_BRIEF_TIME must be HH:MM, got: {settings.brief_time!r}")
    hour, minute = int(parts[0]), int(parts[1])

    scheduler = BackgroundScheduler(timezone=settings.brief_tz)
    scheduler.add_job(
        run_brief_with_drain,
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
