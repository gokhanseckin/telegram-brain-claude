"""Handles Anthropic API call (with prompt caching) and Telegram delivery."""

from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import structlog
from anthropic import Anthropic
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

    return response.content[0].text


def post_to_telegram(text: str) -> None:
    """Post brief text to Telegram via Bot API."""
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
        log.info("brief_posted_to_telegram", status_code=response.status_code)


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
            set_={"summary": brief_text, "generated_at": datetime.now(timezone.utc)},
        )
    )
    session.execute(stmt)
    session.commit()
    log.info("brief_saved_to_db", date=today.isoformat())


def stamp_radar_alerts(session: Session, alert_ids: list[int]) -> None:
    """Stamp surfaced_in_brief_at on all radar alerts included in this brief."""
    if not alert_ids:
        return
    now = datetime.now(timezone.utc)
    session.execute(
        RadarAlert.__table__.update()
        .where(RadarAlert.id.in_(alert_ids))
        .where(RadarAlert.surfaced_in_brief_at.is_(None))
        .values(surfaced_in_brief_at=now)
    )
    session.commit()
    log.info("radar_alerts_stamped", count=len(alert_ids))
