"""Entry point for the tbc-bot service.

Starts aiogram in polling mode. Only responds to messages from the configured
owner user ID.
"""

from __future__ import annotations

import asyncio

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from tbc_common.config import settings
from tbc_common.logging import configure_logging

from tbc_bot.handlers import chat, commands, commitments, feedback, onboarding, retag, tags

log = structlog.get_logger(__name__)


async def main() -> None:
    configure_logging("tg-bot")

    token = settings.tg_bot_token
    if token is None:
        raise RuntimeError("TBC_TG_BOT_TOKEN is not set")
    if settings.tg_owner_user_id is None:
        raise RuntimeError("TBC_TG_OWNER_USER_ID is not set")
    if not settings.mcp_public_url:
        raise RuntimeError("TBC_MCP_PUBLIC_URL is not set")

    provider = settings.llm_provider
    if provider == "anthropic":
        if settings.anthropic_api_key is None:
            raise RuntimeError("ANTHROPIC_API_KEY required when TBC_LLM_PROVIDER=anthropic")
    elif provider == "deepseek":
        if settings.deepseek_api_key is None:
            raise RuntimeError("DEEPSEEK_API_KEY required when TBC_LLM_PROVIDER=deepseek")
    elif provider == "novita":
        if settings.novita_api_key is None:
            raise RuntimeError("NOVITA_API_KEY required when TBC_LLM_PROVIDER=novita")
    else:
        raise RuntimeError(f"Unknown TBC_LLM_PROVIDER: {provider!r}")

    bot = Bot(
        token=token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=None),
    )

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Register routers — chat must be last so commands take priority
    dp.include_router(tags.router)
    dp.include_router(retag.router)
    dp.include_router(onboarding.router)
    dp.include_router(feedback.router)
    dp.include_router(commitments.router)
    dp.include_router(commands.router)
    dp.include_router(chat.router)

    log.info("bot_starting", owner_user_id=settings.tg_owner_user_id)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
