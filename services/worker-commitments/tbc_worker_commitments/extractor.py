"""Commitment extraction job.

Polls message_understanding for rows where is_commitment=True and no
matching row exists in commitments (matched by source_message_id). Creates
commitments rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session
from tbc_common.db import Commitment, MessageUnderstanding
from tbc_common.db.models import Message

logger = structlog.get_logger(__name__)

_ALLOWED_OWNERS = {"user", "counterparty", "user_counterparty"}

# Phrases that indicate the model failed to resolve an antecedent and fell back
# to a generic placeholder. Keeping commitments with these phrases in the brief
# is worse than nothing — the user chases ghosts. Catch them post-LLM and
# rewrite to a deterministic review marker.
_BANNED_RECIPIENT_PHRASES = (
    "the intended recipient",
    "the relevant person",
    "the recipient",
    "the person",
    "the user",
    "someone",
    " him ",
    " her ",
    " them ",
)
_REVIEW_MARKER = "(recipient unclear from context)"


def _sanitize_recipient(what: str) -> tuple[str, bool]:
    """If `what` contains a banned recipient placeholder, replace it with the
    review marker. Returns (sanitized_what, was_modified).
    """
    if not isinstance(what, str) or not what:
        return what, False
    lower = what.lower()
    # Pad with spaces so we catch leading/trailing pronoun matches.
    padded = f" {lower} "
    hit = next((p for p in _BANNED_RECIPIENT_PHRASES if p in padded), None)
    if hit is None:
        return what, False
    # Replace case-insensitively, preserving rest of string.
    import re as _re
    pattern = _re.compile(_re.escape(hit.strip()), _re.IGNORECASE)
    new_what = pattern.sub(_REVIEW_MARKER, what, count=1)
    return new_what, True


def _normalize_owner(raw: Any) -> str:
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v in _ALLOWED_OWNERS:
            return v
    return "counterparty"


def extract_commitments(session: Session) -> int:
    """Find unprocessed commitment rows and create Commitment records.

    Returns the number of new commitments created.
    """
    # Find all MU rows where is_commitment=True
    stmt = select(MessageUnderstanding).where(
        MessageUnderstanding.is_commitment == True,  # noqa: E712
    )
    candidates = list(session.scalars(stmt).all())

    if not candidates:
        logger.debug("no_commitment_candidates")
        return 0

    # Get existing (chat_id, source_message_id) pairs to avoid duplicates.
    # Using chat_id prevents cross-chat collisions (Telegram resets message IDs per chat).
    existing_stmt = select(Commitment.chat_id, Commitment.source_message_id).where(
        Commitment.source_message_id != None,  # noqa: E711
    )
    existing_keys: set[tuple[int, int]] = {(row[0], row[1]) for row in session.execute(existing_stmt).all()}

    created = 0
    for mu in candidates:
        if (mu.chat_id, mu.message_id) in existing_keys:
            continue

        commitment_data: dict[str, Any] = mu.commitment or {}

        confidence = commitment_data.get("confidence")
        raw_what = commitment_data.get("what") or mu.summary_en or "(no description)"
        sanitized_what, was_sanitized = _sanitize_recipient(raw_what)
        if was_sanitized:
            # Penalise: a leak means the model fell back to a placeholder. Drop
            # confidence by 1 so weak (4) leaks fall below the threshold and
            # never become commitments; strong (5) leaks survive but with the
            # review marker in their description.
            if isinstance(confidence, (int, float)):
                confidence = confidence - 1
            logger.info(
                "banned_recipient_phrase_sanitized",
                chat_id=mu.chat_id,
                message_id=mu.message_id,
                original=raw_what,
                rewritten=sanitized_what,
                confidence_after=confidence,
            )

        if not isinstance(confidence, (int, float)) or confidence < 4:
            logger.debug(
                "commitment_skipped_low_confidence",
                chat_id=mu.chat_id,
                message_id=mu.message_id,
                confidence=confidence,
                was_sanitized=was_sanitized,
            )
            continue

        owner = _normalize_owner(commitment_data.get("who"))
        description = sanitized_what

        due_at: datetime | None = None
        due_str = commitment_data.get("due")
        if due_str:
            try:
                parsed = datetime.fromisoformat(due_str)
                due_at = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
            except (ValueError, AttributeError):
                logger.warning("invalid_due_date", due_str=due_str, message_id=mu.message_id)

        # Stamp the commitment with the source message's actual send time so
        # the brief computes true age, not extractor-clock age.
        source_sent_at: datetime | None = session.scalar(
            select(Message.sent_at).where(
                Message.chat_id == mu.chat_id,
                Message.message_id == mu.message_id,
            )
        )

        commitment = Commitment(
            chat_id=mu.chat_id,
            source_message_id=mu.message_id,
            owner=owner,
            description=description,
            due_at=due_at,
            source_sent_at=source_sent_at,
            status="open",
        )
        session.add(commitment)
        existing_keys.add((mu.chat_id, mu.message_id))
        created += 1
        logger.info(
            "commitment_created",
            chat_id=mu.chat_id,
            message_id=mu.message_id,
            owner=owner,
        )

    if created:
        session.commit()

    return created
