"""Owner-only guard for all bot handlers."""

from __future__ import annotations

from aiogram.types import Message

from tbc_common.config import settings


def is_owner(message: Message) -> bool:
    """Return True if the message is from the configured owner user."""
    if message.from_user is None:
        return False
    return message.from_user.id == settings.tg_owner_user_id
