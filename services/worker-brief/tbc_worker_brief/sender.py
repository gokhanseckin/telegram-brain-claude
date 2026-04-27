"""Handles Anthropic API call (with prompt caching) and Telegram delivery."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import cast

import httpx
import structlog
from anthropic import Anthropic
from anthropic.types import TextBlock
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tbc_common.config import settings
from tbc_common.db.models import ChatSummary, RadarAlert
from tbc_common.prompts import BRIEF_SYSTEM

log = structlog.get_logger(__name__)


def call_anthropic(cached_context: str, fresh_input: str) -> str:
    """Call Anthropic API with prompt caching. Returns the brief text."""
    api_key = settings.anthropic_api_key
    if api_key is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    client = Anthropic(api_key=api_key.get_secret_value())

    cached_system_text = BRIEF_SYSTEM

    response = client.messages.create(
        model=settings.brief_model,
        max_tokens=2000,
        system=[
            {
                "type": "text",
                "text": cached_system_text,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": cached_context,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": fresh_input,
                    },
                ],
            }
        ],
    )

    return cast(TextBlock, response.content[0]).text


TELEGRAM_LIMIT = 4096
TELEGRAM_CHUNK_BUDGET = 3900  # leave headroom for HTML entities


def _chunk_for_telegram(text: str) -> list[str]:
    """Split a long brief at line boundaries to fit Telegram's per-message limit."""
    if len(text) <= TELEGRAM_LIMIT:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > TELEGRAM_LIMIT:
        cut = remaining.rfind("\n\n", 0, TELEGRAM_CHUNK_BUDGET)
        if cut == -1:
            cut = remaining.rfind("\n", 0, TELEGRAM_CHUNK_BUDGET)
        if cut == -1:
            cut = TELEGRAM_CHUNK_BUDGET
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def post_to_telegram(text: str) -> None:
    """Post brief text to Telegram via Bot API. Chunks if over the 4096 limit."""
    bot_token = settings.tg_bot_token
    owner_id = settings.tg_owner_user_id

    if bot_token is None or owner_id is None:
        log.warning("Telegram bot token or owner ID not set, skipping delivery")
        return

    url = f"https://api.telegram.org/bot{bot_token.get_secret_value()}/sendMessage"
    chunks = _chunk_for_telegram(text)
    with httpx.Client(timeout=30.0) as client:
        for i, chunk in enumerate(chunks):
            payload = {
                "chat_id": owner_id,
                "text": chunk,
                "parse_mode": "HTML",
            }
            response = client.post(url, json=payload)
            if response.status_code != 200:
                # Retry without HTML parse mode in case markup tripped Telegram.
                payload.pop("parse_mode")
                response = client.post(url, json=payload)
            response.raise_for_status()
            log.info(
                "brief_posted_to_telegram",
                status_code=response.status_code,
                chunk=f"{i + 1}/{len(chunks)}",
                length=len(chunk),
            )


def save_brief(session: Session, brief_text: str, today: date) -> None:
    """Write brief to chat_summaries with chat_id=0, period='brief'."""
    stmt = (
        pg_insert(ChatSummary)
        .values(
            chat_id=0,
            period="brief",
            period_start=today,
            summary=brief_text,
        )
        .on_conflict_do_update(
            index_elements=["chat_id", "period", "period_start"],
            set_={"summary": brief_text, "generated_at": datetime.now(UTC)},
        )
    )
    session.execute(stmt)
    session.commit()
    log.info("brief_saved_to_db", date=today.isoformat())


def stamp_radar_alerts(session: Session, alert_ids: list[int]) -> None:
    """Stamp surfaced_in_brief_at on all radar alerts included in this brief."""
    if not alert_ids:
        return
    now = datetime.now(UTC)
    session.execute(
        RadarAlert.__table__.update()
        .where(RadarAlert.id.in_(alert_ids))
        .where(RadarAlert.surfaced_in_brief_at.is_(None))
        .values(surfaced_in_brief_at=now)
    )
    session.commit()
    log.info("radar_alerts_stamped", count=len(alert_ids))
