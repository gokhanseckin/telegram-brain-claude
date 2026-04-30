"""Handles Anthropic Batch API call and Telegram delivery for weekly review."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from typing import cast

import httpx
import structlog
from anthropic import Anthropic
from anthropic.types.beta import BetaTextBlock
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tbc_common.config import settings
from tbc_common.db.models import ChatSummary

log = structlog.get_logger(__name__)

BATCH_POLL_INTERVAL = 60  # seconds
BATCH_TIMEOUT = 600  # seconds (10 minutes)


def call_llm(weekly_input: str, today: date, *, system_prompt: str) -> str:
    """Call the configured LLM provider. Returns the weekly review text."""
    provider = settings.llm_provider
    if provider == "deepseek":
        return _call_deepseek(weekly_input, system_prompt=system_prompt)
    if provider == "anthropic":
        return _call_anthropic_batch(weekly_input, today, system_prompt=system_prompt)
    raise ValueError(f"Unknown LLM provider: {provider!r}")


def _call_anthropic_batch(weekly_input: str, today: date, *, system_prompt: str) -> str:
    """Anthropic Batch API path with polling."""
    api_key = settings.anthropic_api_key
    if api_key is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    client = Anthropic(api_key=api_key.get_secret_value())

    batch = client.beta.messages.batches.create(
        requests=[
            {
                "custom_id": f"weekly-{today.isoformat()}",
                "params": {
                    "model": settings.brief_model,
                    "max_tokens": 4000,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": weekly_input}],
                },
            }
        ]
    )

    log.info("batch_submitted", batch_id=batch.id)

    # Poll for completion
    deadline = time.monotonic() + BATCH_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(BATCH_POLL_INTERVAL)
        batch = client.beta.messages.batches.retrieve(batch.id)
        log.info("batch_status_check", batch_id=batch.id, processing_status=batch.processing_status)
        if batch.processing_status == "ended":
            break
    else:
        raise TimeoutError(f"Batch {batch.id} did not complete within {BATCH_TIMEOUT}s")

    # Retrieve results
    results = list(client.beta.messages.batches.results(batch.id))
    if not results:
        raise RuntimeError(f"Batch {batch.id} returned no results")

    result = results[0]
    if result.result.type != "succeeded":
        raise RuntimeError(f"Batch request failed: {result.result}")

    return cast(BetaTextBlock, result.result.message.content[0]).text


def _call_deepseek(weekly_input: str, *, system_prompt: str) -> str:
    """DeepSeek path via OpenAI-compatible API."""
    from openai import OpenAI

    api_key = settings.deepseek_api_key
    if api_key is None:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")

    client = OpenAI(
        api_key=api_key.get_secret_value(),
        base_url="https://api.deepseek.com",
    )
    response = client.chat.completions.create(
        model="deepseek-chat",
        max_tokens=4000,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": weekly_input},
        ],
    )
    return response.choices[0].message.content or ""


def post_to_telegram(text: str) -> None:
    """Post weekly review to Telegram via Bot API."""
    bot_token = settings.tg_bot_token
    owner_id = settings.tg_owner_user_id

    if bot_token is None or owner_id is None:
        log.warning("Telegram bot token or owner ID not set, skipping delivery")
        return

    url = f"https://api.telegram.org/bot{bot_token.get_secret_value()}/sendMessage"
    payload = {
        "chat_id": owner_id,
        "text": text,
        "parse_mode": "HTML",
    }

    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        log.info("weekly_posted_to_telegram", status_code=response.status_code)


def save_weekly(session: Session, weekly_text: str, monday: date) -> None:
    """Write weekly review to chat_summaries with chat_id=0, period='week'."""
    stmt = (
        pg_insert(ChatSummary)
        .values(
            chat_id=0,
            period="week",
            period_start=monday,
            summary=weekly_text,
        )
        .on_conflict_do_update(
            index_elements=["chat_id", "period", "period_start"],
            set_={"summary": weekly_text, "generated_at": datetime.now(UTC)},
        )
    )
    session.execute(stmt)
    session.commit()
    log.info("weekly_saved_to_db", monday=monday.isoformat())
