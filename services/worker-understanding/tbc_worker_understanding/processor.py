"""Per-message understanding + embedding + DB write."""

from __future__ import annotations

import json
import re
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tbc_common.db.models import Message, MessageUnderstanding
from tbc_common.prompts import MODEL_VERSION

from .ollama_client import OllamaClient
from .schema import UnderstandingOutput

log = structlog.get_logger(__name__)

def _extract_json_object(raw: str) -> str | None:
    """Strip markdown fences and extract the first balanced JSON object."""
    s = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
    return None


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


async def _mark_parse_failed(
    message: Message,
    session: Session,
    ollama: OllamaClient,
    embedding_model: str,
) -> None:
    """Persist a minimal row so the message is not retried forever and queue advances.

    Uses the current MODEL_VERSION (so the poll skips it) but leaves understanding
    fields NULL. Identifiable later via `summary_en IS NULL AND embedding IS NOT NULL`.
    """
    embedding = await ollama.embed(model=embedding_model, input=message.text or "")
    stmt = (
        pg_insert(MessageUnderstanding)
        .values(
            chat_id=message.chat_id,
            message_id=message.message_id,
            model_version=MODEL_VERSION,
            embedding=embedding,
        )
        .on_conflict_do_update(
            index_elements=["chat_id", "message_id"],
            set_={
                "model_version": MODEL_VERSION,
                "embedding": embedding,
                "processed_at": text("now()"),
            },
        )
    )
    session.execute(stmt)
    session.commit()
    log.info(
        "parse_failed_persisted",
        chat_id=message.chat_id,
        message_id=message.message_id,
    )


async def process_message(
    message: Message,
    session: Session,
    ollama: OllamaClient,
    understanding_model: str,
    embedding_model: str,
    *,
    system_prompt: str,
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
        system=system_prompt,
        user=user_input,
    )

    # --- 3. Parse JSON (tolerate markdown fences and surrounding prose) ---
    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        extracted = _extract_json_object(raw_content)
        if extracted is not None:
            try:
                parsed = json.loads(extracted)
            except json.JSONDecodeError:
                parsed = None

    if parsed is None:
        log.warning(
            "malformed_json_from_ollama",
            chat_id=message.chat_id,
            message_id=message.message_id,
            raw=raw_content[:200],
        )
        await _mark_parse_failed(message, session, ollama, embedding_model)
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
        await _mark_parse_failed(message, session, ollama, embedding_model)
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
