"""Entry point for the tbc-bot service.

Starts aiogram in polling mode. Only responds to messages from the configured
owner user ID.
"""

from __future__ import annotations

import asyncio

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from tbc_common.config import settings
from tbc_common.logging import configure_logging

from tbc_bot.handlers import commands, feedback, onboarding

log = structlog.get_logger(__name__)


async def main() -> None:
    configure_logging("tg-bot")

    token = settings.tg_bot_token
    if token is None:
        raise RuntimeError("TBC_TG_BOT_TOKEN is not set")

    bot = Bot(
        token=token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Register routers
    dp.include_router(onboarding.router)
    dp.include_router(feedback.router)
    dp.include_router(commands.router)

    log.info("bot_starting", owner_user_id=settings.tg_owner_user_id)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
