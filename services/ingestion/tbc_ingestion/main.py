"""Main entrypoint for the ingestion service.

Run with:
    python -m tbc_ingestion.main

First-run interactive auth:
    On the first run, Telethon will prompt for:
    1. Your phone number (international format, e.g. +1234567890)
    2. The SMS/Telegram code sent to your account
    3. Your 2FA cloud password (if enabled — it should be)

    After successful auth the session is persisted to the path defined by
    TBC_TG_SESSION_PATH. Subsequent runs are non-interactive.

    In production, wrap the session file with `age` encryption (see client.py
    for details). This service only reads/writes the path as-is.
"""

from __future__ import annotations

import asyncio

import structlog
from tbc_common.logging import configure_logging
from telethon.errors import (
    FloodWaitError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from .client import build_client
from .gap_recovery import run_gap_recovery
from .handlers import register_handlers
from .initial_backfill import run_initial_backfill
from .pause import start_pause_monitor

log = structlog.get_logger(__name__)


async def _async_main() -> None:
    configure_logging("ingestion")
    log.info("ingestion_service_starting")

    client = build_client()

    try:
        await client.start()
    except SessionPasswordNeededError:
        log.error(
            "2fa_password_required",
            hint="Run interactively and enter your 2FA cloud password when prompted.",
        )
        raise
    except PhoneCodeInvalidError:
        log.error("phone_code_invalid", hint="The SMS/Telegram code you entered was wrong.")
        raise
    except FloodWaitError as e:
        log.error("flood_wait_on_start", wait_seconds=e.seconds)
        raise

    log.info("telegram_client_connected")

    # Start the pause-file monitor as a background task.
    asyncio.create_task(start_pause_monitor())

    # One-time 30-day backfill on first deploy (no-op after first success).
    await run_initial_backfill(client)

    # Back-fill any messages that arrived while the service was offline.
    log.info("starting_gap_recovery")
    await run_gap_recovery(client)

    # Register live event handlers.
    register_handlers(client)
    log.info("live_handlers_registered")

    # Block until disconnected (Ctrl-C or Telegram disconnects us).
    log.info("ingestion_service_running")
    await client.run_until_disconnected()

    log.info("ingestion_service_stopped")


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
