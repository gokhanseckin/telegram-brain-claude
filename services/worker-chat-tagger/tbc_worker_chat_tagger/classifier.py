"""Stage A → Stage B orchestration. Writes Chat.tag and friends."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from tbc_common.config import settings
from tbc_common.db.models import Chat, Message

from tbc_worker_chat_tagger.centroids import (
    ClassificationResult,
    TagCentroid,
    build_tag_centroids,
    chat_centroid,
)
from tbc_worker_chat_tagger.prompts import CHAT_TAGGER_SYSTEM, build_user_prompt

log = structlog.get_logger(__name__)


@dataclass
class TagDecision:
    tag: str
    confidence: float
    source: str  # 'auto_embedding' | 'auto_llm'
    reason: str


# --- JSON extraction (replicates worker-understanding helper) ---

def _extract_json_object(raw: str) -> str | None:
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


def _parse_llm_json(raw: str) -> dict[str, Any] | None:
    try:
        result: dict[str, Any] = json.loads(raw)
        return result
    except json.JSONDecodeError:
        extracted = _extract_json_object(raw)
        if extracted is None:
            return None
        try:
            result = json.loads(extracted)
            return result
        except json.JSONDecodeError:
            return None


# --- Stage B: Ollama call ---

VALID_TAGS = {
    "client", "prospect", "supplier", "partner", "internal",
    "friend", "family", "personal", "ignore",
}


async def _ollama_chat(system: str, user: str) -> str:
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{settings.ollama_base_url}/api/chat",
            json={
                "model": settings.understanding_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content: str = data["message"]["content"]
        return content


def _gather_messages(session: Session, chat_id: int, limit: int = 20) -> list[str]:
    rows = session.execute(
        select(Message.text)
        .where(Message.chat_id == chat_id)
        .where(Message.text.isnot(None))
        .order_by(Message.sent_at.desc())
        .limit(limit)
    ).all()
    return [r[0] for r in reversed(rows) if r[0]]


def _gather_examples(
    session: Session, per_tag: int = 2, msgs_per_chat: int = 5
) -> dict[str, list[list[str]]]:
    """Pull a few message snippets from manually-tagged chats per tag."""
    examples: dict[str, list[list[str]]] = {}
    chats = session.execute(
        select(Chat.chat_id, Chat.tag, Chat.title)
        .where(Chat.tag.isnot(None))
        .where(Chat.tag_source == "manual")
    ).all()

    for chat_id, tag, _title in chats:
        if tag == "ignore":
            continue
        if len(examples.get(tag, [])) >= per_tag:
            continue
        msgs = _gather_messages(session, chat_id, limit=msgs_per_chat)
        if not msgs:
            continue
        examples.setdefault(tag, []).append(msgs)
    return examples


async def _classify_with_llm(
    session: Session, chat: Chat
) -> TagDecision | None:
    target = _gather_messages(session, chat.chat_id, limit=20)
    if not target:
        return None
    examples = _gather_examples(session)
    user_prompt = build_user_prompt(
        chat_title=chat.title or f"chat_{chat.chat_id}",
        target_messages=target,
        examples=examples,
    )
    try:
        raw = await _ollama_chat(CHAT_TAGGER_SYSTEM, user_prompt)
    except Exception:
        log.exception("ollama_chat_failed", chat_id=chat.chat_id)
        return None

    parsed = _parse_llm_json(raw)
    if not parsed or "tag" not in parsed:
        log.warning("malformed_llm_json", chat_id=chat.chat_id, raw=raw[:200])
        return None
    tag = parsed["tag"]
    if tag not in VALID_TAGS:
        log.warning("invalid_tag_from_llm", chat_id=chat.chat_id, tag=tag)
        return None
    confidence = float(parsed.get("confidence", 0.5))
    reason = str(parsed.get("reason", "")).strip()
    return TagDecision(
        tag=tag, confidence=confidence, source="auto_llm", reason=reason
    )


# --- Stage A wrapper ---

def _classify_with_embeddings(
    session: Session, chat: Chat, tag_centroids: list[TagCentroid]
) -> TagDecision | None:
    centroid, n = chat_centroid(session, chat.chat_id, settings.tagger_sample_size)
    if centroid is None or n < settings.tagger_min_messages:
        return None
    result = None
    if tag_centroids:
        result = _classify_pure(centroid, tag_centroids)
    if result is None:
        return None
    if (
        result.similarity < settings.tagger_auto_threshold
        or result.margin < settings.tagger_margin
    ):
        return None
    return TagDecision(
        tag=result.tag,
        confidence=result.similarity,
        source="auto_embedding",
        reason=f"Closest tag centroid (sim={result.similarity:.3f}, margin={result.margin:.3f})",
    )


def _classify_pure(
    chat_vec: list[float], tag_centroids: list[TagCentroid]
) -> ClassificationResult | None:
    from tbc_worker_chat_tagger.centroids import classify
    return classify(chat_vec, tag_centroids)


# --- Top-level orchestration ---

_INVOLVEMENT_DAYS = 180
"""Match the ingestion backfill window. Older messages aren't in the DB anyway."""


def candidate_chats(session: Session) -> list[Chat]:
    """Chats eligible for auto-tagging this run.

    Strict rule: only tag chats where the owner has direct involvement
    within the ingestion window (last 180 days). Involvement means:
      - The owner sent at least one message in the chat, OR
      - The owner was mentioned by @username, OR
      - Someone replied to a message the owner sent.

    Anything else (silent group memberships, broadcast feeds, accidental
    invites, bot pings) is left untagged. A 96%-noise filter on the
    candidate pool — see audit on 2026-04-29.
    """
    if settings.tg_owner_user_id is None or not settings.tg_owner_username:
        log.warning(
            "tagger_owner_unset",
            note="tg_owner_user_id and tg_owner_username must be set; skipping run",
        )
        return []

    cutoff = datetime.now(UTC) - timedelta(days=_INVOLVEMENT_DAYS)
    owner_id = settings.tg_owner_user_id
    mention_pattern = f"%@{settings.tg_owner_username}%"

    # Chat ids where owner sent something within the window.
    sent = (
        select(Message.chat_id)
        .where(Message.sender_id == owner_id)
        .where(Message.sent_at >= cutoff)
        .where(Message.deleted_at.is_(None))
    )

    # Chat ids where owner was @-mentioned in text within the window.
    mentioned = (
        select(Message.chat_id)
        .where(Message.text.ilike(mention_pattern))
        .where(Message.sent_at >= cutoff)
        .where(Message.deleted_at.is_(None))
    )

    # Chat ids where someone replied to a message the owner sent (chat_id +
    # message_id scoped — Telegram message ids reset per chat).
    parent = Message.__table__.alias("parent")
    replied = (
        select(Message.chat_id)
        .join(
            parent,
            (parent.c.chat_id == Message.chat_id)
            & (parent.c.message_id == Message.reply_to_id),
        )
        .where(parent.c.sender_id == owner_id)
        .where(Message.sent_at >= cutoff)
        .where(Message.deleted_at.is_(None))
    )

    involved = sent.union(mentioned, replied).subquery()

    rows = session.execute(
        select(Chat)
        .where(Chat.tag_locked.is_(False))
        .where(Chat.tag.is_(None))
        .where(Chat.chat_id.in_(select(involved)))
    ).scalars().all()
    return list(rows)


def _write_decision(session: Session, chat: Chat, decision: TagDecision) -> None:
    session.execute(
        text(
            """
            UPDATE chats
            SET tag = :tag,
                tag_confidence = :confidence,
                tag_source = :source,
                tag_reason = :reason,
                tag_set_at = :now
            WHERE chat_id = :chat_id
              AND tag_locked = FALSE
            """
        ),
        {
            "tag": decision.tag,
            "confidence": decision.confidence,
            "source": decision.source,
            "reason": decision.reason,
            "now": datetime.now(UTC),
            "chat_id": chat.chat_id,
        },
    )
    session.commit()


def run_once(session: Session) -> dict[str, int]:
    """One sweep over all eligible chats. Returns counters.

    Emits a progress log every 25 chats so a long sweep (potentially hours
    on Stage B) is observable in journalctl.
    """
    import time as _time

    counters = {"considered": 0, "tagged_a": 0, "tagged_b": 0, "skipped": 0}
    tag_centroids = build_tag_centroids(session, settings.tagger_sample_size)
    all_candidates = candidate_chats(session)
    cap = settings.tagger_max_per_run
    candidates = all_candidates[:cap] if cap > 0 else all_candidates
    total = len(candidates)
    log.info(
        "tagger_run_starting",
        total=total,
        candidates_pending=len(all_candidates),
        cap=cap,
        centroid_tags=[tc.tag for tc in tag_centroids],
    )

    started = _time.monotonic()
    PROGRESS_EVERY = 25

    for i, chat in enumerate(candidates, start=1):
        counters["considered"] += 1
        # No message-count floor: candidate_chats already required owner
        # involvement (sent / mentioned / replied-to). If the owner has
        # touched a chat at all, it's worth a tag — even at 1 message.
        decision = _classify_with_embeddings(session, chat, tag_centroids)
        if decision is not None:
            _write_decision(session, chat, decision)
            counters["tagged_a"] += 1
            log.info(
                "chat_tagged_embedding",
                chat_id=chat.chat_id,
                tag=decision.tag,
                confidence=decision.confidence,
            )
        else:
            # Stage B: LLM fallback (handles small / unembedded chats)
            try:
                decision = asyncio.run(_classify_with_llm(session, chat))
            except Exception:
                log.exception("llm_classification_failed", chat_id=chat.chat_id)
                decision = None

            if decision is not None:
                _write_decision(session, chat, decision)
                counters["tagged_b"] += 1
                log.info(
                    "chat_tagged_llm",
                    chat_id=chat.chat_id,
                    tag=decision.tag,
                    confidence=decision.confidence,
                    reason=decision.reason,
                )
            else:
                counters["skipped"] += 1

        if i % PROGRESS_EVERY == 0 or i == total:
            elapsed = _time.monotonic() - started
            rate = i / elapsed if elapsed > 0 else 0.0
            remaining = (total - i) / rate if rate > 0 else 0.0
            log.info(
                "tagger_progress",
                done=i,
                total=total,
                rate_per_sec=round(rate, 3),
                eta_seconds=int(remaining),
                **counters,
            )

    log.info("tagger_run_done", **counters)
    return counters
