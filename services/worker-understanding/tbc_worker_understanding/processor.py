"""Per-message understanding + embedding + DB write."""

from __future__ import annotations

import json
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tbc_common.db.models import Message, MessageUnderstanding
from tbc_common.prompts import MODEL_VERSION, UNDERSTANDING_SYSTEM

from .ollama_client import OllamaClient
from .schema import UnderstandingOutput

log = structlog.get_logger(__name__)


def _build_user_message(message: Message, context_messages: list[Message]) -> str:
    """Build the user-facing input string for the understanding prompt."""
    lines: list[str] = []
    if context_messages:
        lines.append("=== Prior context (oldest first) ===")
        for m in context_messages:
            lines.append(f"[{m.sent_at.isoformat()}] {m.text}")
        lines.append("")
    lines.append("=== Message to analyse ===")
    lines.append(message.text or "")
    return "\n".join(lines)


async def process_message(
    message: Message,
    session: Session,
    ollama: OllamaClient,
    understanding_model: str,
    embedding_model: str,
) -> None:
    """Run the understanding + embedding pipeline and persist results."""

    # --- 1. Fetch 3 prior messages in the same chat for context ---
    prior = (
        session.execute(
            select(Message)
            .where(
                Message.chat_id == message.chat_id,
                Message.message_id < message.message_id,
                Message.deleted_at.is_(None),
                Message.text.isnot(None),
                Message.text != "",
            )
            .order_by(Message.sent_at.desc())
            .limit(3)
        )
        .scalars()
        .all()
    )
    context_messages = list(reversed(prior))  # oldest first

    user_input = _build_user_message(message, context_messages)

    # --- 2. Call understanding model ---
    raw_content = await ollama.chat(
        model=understanding_model,
        system=UNDERSTANDING_SYSTEM,
        user=user_input,
    )

    # --- 3. Parse JSON ---
    try:
        parsed: dict[str, Any] = json.loads(raw_content)
    except json.JSONDecodeError:
        log.warning(
            "malformed_json_from_ollama",
            chat_id=message.chat_id,
            message_id=message.message_id,
            raw=raw_content[:200],
        )
        return

    # --- 4. Validate against schema ---
    try:
        output = UnderstandingOutput.model_validate(parsed)
    except ValidationError as exc:
        log.warning(
            "validation_error_from_ollama",
            chat_id=message.chat_id,
            message_id=message.message_id,
            error=str(exc),
        )
        return

    # --- 5. Embedding ---
    embedding = await ollama.embed(model=embedding_model, input=message.text or "")

    # --- 6. Upsert into message_understanding ---
    stmt = (
        pg_insert(MessageUnderstanding)
        .values(
            chat_id=message.chat_id,
            message_id=message.message_id,
            model_version=MODEL_VERSION,
            language=output.language,
            entities=output.entities,
            intent=output.intent,
            is_directed_at_user=output.is_directed_at_user,
            is_commitment=output.is_commitment,
            commitment=output.commitment,
            is_signal=output.is_signal,
            signal_type=output.signal_type,
            signal_strength=output.signal_strength,
            sentiment_delta=output.sentiment_delta,
            summary_en=output.summary_en,
            embedding=embedding,
        )
        .on_conflict_do_update(
            index_elements=["chat_id", "message_id"],
            set_={
                "model_version": MODEL_VERSION,
                "language": output.language,
                "entities": output.entities,
                "intent": output.intent,
                "is_directed_at_user": output.is_directed_at_user,
                "is_commitment": output.is_commitment,
                "commitment": output.commitment,
                "is_signal": output.is_signal,
                "signal_type": output.signal_type,
                "signal_strength": output.signal_strength,
                "sentiment_delta": output.sentiment_delta,
                "summary_en": output.summary_en,
                "embedding": embedding,
                "processed_at": text("now()"),
            },
        )
    )
    session.execute(stmt)
    session.commit()

    log.info(
        "message_processed",
        chat_id=message.chat_id,
        message_id=message.message_id,
        model_version=MODEL_VERSION,
        intent=output.intent,
    )
