"""Brief feedback write tool — natural-language counterpart to /feedback slash."""

from __future__ import annotations

from datetime import date as date_cls

import structlog
from sqlalchemy.orm import Session
from tbc_common.db.models import ALLOWED_FEEDBACK_TYPES, BriefFeedback

from ..models import BriefFeedbackResult

log = structlog.get_logger(__name__)

# Re-exported for backward compatibility — callers can keep importing
# `from tbc_mcp_server.tools.feedback import ALLOWED_FEEDBACK_TYPES`.
__all__ = ["ALLOWED_FEEDBACK_TYPES", "InvalidFeedbackType", "write_brief_feedback"]


class InvalidFeedbackType(ValueError):
    """Raised when feedback_type isn't one of the allowed values."""


def _normalize_item_ref(item_ref: str | None) -> str | None:
    if item_ref is None:
        return None
    cleaned = item_ref.strip().lstrip("#").lower()
    return cleaned or None


def write_brief_feedback(
    db: Session,
    feedback_type: str,
    item_ref: str | None = None,
    note: str | None = None,
    brief_date: date_cls | None = None,
) -> BriefFeedbackResult:
    """Record user feedback on a brief item.

    Mirrors the row shape produced by /feedback (handlers/feedback.py) so both
    paths populate `brief_feedback` identically.

    - feedback_type must be one of: useful, not_useful, missed_important.
    - item_ref is the `#xxxx` tag from the brief (without the `#`); pass None
      when the user is reporting a missed item that has no tag.
    - brief_date defaults to today (UTC date, matching the slash handler).
    """
    if feedback_type not in ALLOWED_FEEDBACK_TYPES:
        raise InvalidFeedbackType(
            f"feedback_type must be one of {ALLOWED_FEEDBACK_TYPES}, got {feedback_type!r}"
        )

    ref = _normalize_item_ref(item_ref)
    cleaned_note = note.strip() if note else None
    cleaned_note = cleaned_note or None

    row = BriefFeedback(
        brief_date=brief_date or date_cls.today(),
        item_ref=ref,
        feedback=feedback_type,
        note=cleaned_note,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    log.info(
        "brief_feedback_written",
        id=row.id,
        item_ref=ref,
        feedback=feedback_type,
        via="mcp",
    )

    return BriefFeedbackResult(
        id=row.id,
        brief_date=row.brief_date,
        item_ref=row.item_ref,
        feedback=row.feedback,
        note=row.note,
        created_at=row.created_at,
    )
