"""Per-message understanding + embedding + DB write."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tbc_common.db.models import Commitment, Message, MessageUnderstanding, User
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


def _build_user_message(
    message: Message,
    context_messages: list[Message],
    sender_labels: dict[int | None, str],
) -> str:
    """Build the user-facing input string for the understanding prompt."""
    lines: list[str] = []
    if context_messages:
        lines.append("=== Prior context (oldest first) ===")
        for m in context_messages:
            label = sender_labels.get(m.sender_id, "unknown")
            lines.append(f"[{m.sent_at.isoformat()}] [{label}] {m.text}")
        lines.append("")
    lines.append("=== Message to analyse ===")
    label = sender_labels.get(message.sender_id, "unknown")
    lines.append(f"[{label}] {message.text or ''}")
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
            .limit(int(os.environ.get("TBC_UNDERSTANDING_PRIOR_CONTEXT_N", "7")))
        )
        .scalars()
        .all()
    )
    context_messages = list(reversed(prior))  # oldest first

    all_sender_ids = {m.sender_id for m in context_messages} | {message.sender_id}
    all_sender_ids.discard(None)
    sender_labels: dict[int | None, str] = {}
    for sid in all_sender_ids:
        user = session.get(User, sid)
        if user is None:
            sender_labels[sid] = "unknown"
        elif user.is_self:
            sender_labels[sid] = "YOU"
        else:
            name = " ".join(filter(None, [user.first_name, user.last_name])) or user.username or str(sid)
            sender_labels[sid] = name

    user_input = _build_user_message(message, context_messages, sender_labels)

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

async def process_message_batch(
    messages: list[Message],
    session: Session,
    ollama: OllamaClient,
    understanding_model: str,
    embedding_model: str,
    *,
    system_prompt_batched: str,
) -> int:
    """Send a batch of messages in a single LLM call. Returns count successfully processed."""
    if not messages:
        return 0

    blocks: list[str] = []
    for idx, message in enumerate(messages, start=1):
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
                .limit(int(os.environ.get("TBC_UNDERSTANDING_PRIOR_CONTEXT_N", "7")))
            )
            .scalars()
            .all()
        )
        context_messages = list(reversed(prior))

        all_sender_ids = {m.sender_id for m in context_messages} | {message.sender_id}
        all_sender_ids.discard(None)
        sender_labels: dict[int | None, str] = {}
        for sid in all_sender_ids:
            user = session.get(User, sid)
            if user is None:
                sender_labels[sid] = "unknown"
            elif user.is_self:
                sender_labels[sid] = "YOU"
            else:
                name = " ".join(filter(None, [user.first_name, user.last_name])) or user.username or str(sid)
                sender_labels[sid] = name

        block_lines = [f"=== Message #{idx} ==="]
        if context_messages:
            block_lines.append("=== Prior context (oldest first) ===")
            for m in context_messages:
                lab = sender_labels.get(m.sender_id, "unknown")
                block_lines.append(f"[{m.sent_at.isoformat()}] [{lab}] {m.text}")
            block_lines.append("")
        block_lines.append("=== Message to analyse ===")
        lab = sender_labels.get(message.sender_id, "unknown")
        block_lines.append(f"[{lab}] {message.text or ''}")
        blocks.append("\n".join(block_lines))

    user_input = "\n\n".join(blocks)

    raw_content = await ollama.chat_batch(system=system_prompt_batched, user=user_input)

    parsed: dict | None = None
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        extracted = _extract_json_object(raw_content)
        if extracted is not None:
            try:
                parsed = json.loads(extracted)
            except json.JSONDecodeError:
                parsed = None

    if parsed is None or not isinstance(parsed, dict) or "results" not in parsed:
        log.warning(
            "batch_parse_failed",
            n=len(messages),
            raw=(raw_content or "")[:300],
        )
        for m in messages:
            await _mark_parse_failed(m, session, ollama, embedding_model)
        return 0

    results_raw = parsed.get("results")
    if not isinstance(results_raw, list):
        log.warning("batch_results_not_list", n=len(messages))
        for m in messages:
            await _mark_parse_failed(m, session, ollama, embedding_model)
        return 0

    # Match results by their echoed "id" field. Position-fallback: use index+1.
    by_id: dict[int, dict] = {}
    for i, obj in enumerate(results_raw, start=1):
        if not isinstance(obj, dict):
            continue
        rid = obj.get("id", i)
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            rid = i
        if 1 <= rid <= len(messages) and rid not in by_id:
            by_id[rid] = obj

    log.info(
        "batch_results_received",
        expected=len(messages),
        got=len(results_raw),
        matched=len(by_id),
    )

    embeddings = await ollama.embed_batch(model=embedding_model, inputs=[m.text or "" for m in messages])

    # Build a map idx -> message for resolution lookups
    idx_to_message = {i + 1: m for i, m in enumerate(messages)}
    pending_resolves: list[tuple[int, int]] = []  # (resolver_idx, target_idx)

    success = 0
    for idx, (message, emb) in enumerate(zip(messages, embeddings), start=1):
        raw_obj = by_id.get(idx)
        if raw_obj is None:
            log.warning(
                "batch_missing_result",
                chat_id=message.chat_id,
                message_id=message.message_id,
                slot=idx,
            )
            await _mark_parse_failed(message, session, ollama, embedding_model)
            continue
        # Capture resolves before validation in case schema rejects unknown fields.
        resolves_raw = raw_obj.get("resolves") if isinstance(raw_obj, dict) else None
        try:
            resolves_idx = int(resolves_raw) if resolves_raw is not None else None
        except (TypeError, ValueError):
            resolves_idx = None
        if resolves_idx is not None and resolves_idx != idx and 1 <= resolves_idx < idx:
            pending_resolves.append((idx, resolves_idx))

        # Strip resolves before pydantic validation
        if isinstance(raw_obj, dict) and "resolves" in raw_obj:
            raw_obj = {k: v for k, v in raw_obj.items() if k != "resolves"}

        try:
            output = UnderstandingOutput.model_validate(raw_obj)
        except ValidationError as exc:
            log.warning(
                "batch_element_invalid",
                chat_id=message.chat_id,
                message_id=message.message_id,
                error=str(exc)[:200],
            )
            await _mark_parse_failed(message, session, ollama, embedding_model)
            continue

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
                embedding=emb,
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
                    "embedding": emb,
                    "processed_at": text("now()"),
                },
            )
        )
        session.execute(stmt)
        success += 1

    # Apply in-batch resolutions: for each (resolver_idx -> target_idx) pair,
    # mark any open commitment whose source_message matches target as resolved.
    resolved_count = 0
    for resolver_idx, target_idx in pending_resolves:
        resolver_msg = idx_to_message.get(resolver_idx)
        target_msg = idx_to_message.get(target_idx)
        if resolver_msg is None or target_msg is None:
            continue
        if resolver_msg.chat_id != target_msg.chat_id:
            continue
        existing = session.execute(
            select(Commitment).where(
                Commitment.chat_id == target_msg.chat_id,
                Commitment.source_message_id == target_msg.message_id,
                Commitment.status == "open",
            )
        ).scalars().first()
        if existing is not None:
            existing.status = "resolved"
            existing.resolved_at = datetime.now(UTC)
            existing.resolved_by_message_id = resolver_msg.message_id
            resolved_count += 1
            log.info(
                "commitment_auto_resolved",
                commitment_id=existing.id,
                resolved_by_chat=resolver_msg.chat_id,
                resolved_by_message=resolver_msg.message_id,
            )

    session.commit()

    log.info(
        "batch_processed",
        n=len(messages),
        success=success,
        commitments=sum(1 for r in results_raw if isinstance(r, dict) and r.get("is_commitment")),
    )
    return success

