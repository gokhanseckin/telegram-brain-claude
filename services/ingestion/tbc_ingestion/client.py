"""TelegramClient factory.

Creates and returns a configured Telethon TelegramClient using credentials
from tbc_common.config.settings.

Session file handling notes:
- In development: `settings.tg_session_path` points to a plain `.session` file.
- In production: wrap the session file with `age` encryption
  (https://github.com/FiloSottile/age). Store the age-encrypted session on disk
  and decrypt to a tmpfs location at startup using an age identity key held
  outside the repo (e.g. in a Hetzner secret or systemd credential).
  This service does NOT implement age crypto — it assumes the session path
  already points to the decrypted file.

Interactive first-run auth:
- On the very first run the session file does not exist. Telethon will prompt
  for a phone number, SMS code, and (if 2FA is enabled) the cloud password.
  This is the only non-automatable setup step; run `python -m tbc_ingestion.main`
  interactively once, then the session persists.
"""

from __future__ import annotations

import structlog
from tbc_common.config import settings
from telethon import TelegramClient

log = structlog.get_logger(__name__)


def build_client() -> TelegramClient:
    """Return a TelegramClient instance (not yet connected)."""
    if settings.tg_api_id is None or settings.tg_api_hash is None:
        raise RuntimeError(
            "TBC_TG_API_ID and TBC_TG_API_HASH must be set in the environment."
        )

    session_path = settings.tg_session_path
    # Strip .session suffix if present — Telethon appends it automatically.
    if session_path.endswith(".session"):
        session_path = session_path[: -len(".session")]

    log.info("building_telegram_client", session_path=session_path)

    return TelegramClient(
        session=session_path,
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash.get_secret_value(),
        # Telethon's default connection retries; we handle FloodWaitError ourselves.
        connection_retries=5,
    )
