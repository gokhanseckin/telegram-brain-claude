"""Pending-understanding queue helpers shared between workers.

The understanding worker uses this to pick the next batch of messages to
process; the brief worker uses it to count what's still pending so it can
wait for the queue to drain before generating a brief.

Both queries are deliberately identical in their WHERE clauses so the
counter and the row picker agree on what "pending" means.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

_PENDING_WHERE = """
    mu.message_id IS NULL
      AND m.deleted_at IS NULL
      AND m.text IS NOT NULL
      AND m.text != ''
      AND m.sent_at >= NOW() - INTERVAL '3 days'
      AND m.chat_id IN (
          SELECT chat_id FROM chats
          WHERE tag IS NOT NULL AND tag != 'ignore'
      )
"""

_ROWS_SQL = text(f"""
    SELECT m.chat_id, m.message_id
    FROM messages m
    LEFT JOIN message_understanding mu
      ON mu.chat_id = m.chat_id
     AND mu.message_id = m.message_id
     AND mu.model_version = :model_version
    WHERE {_PENDING_WHERE}
    ORDER BY m.sent_at DESC
    LIMIT 200
""")

_COUNT_SQL = text(f"""
    SELECT COUNT(*)
    FROM messages m
    LEFT JOIN message_understanding mu
      ON mu.chat_id = m.chat_id
     AND mu.message_id = m.message_id
     AND mu.model_version = :model_version
    WHERE {_PENDING_WHERE}
""")


def pending_understanding_rows(
    session: Session, *, model_version: str
) -> list[tuple[int, int]]:
    """Return up to 200 (chat_id, message_id) pairs newest-first that have
    no message_understanding row at the given model_version yet."""
    rows = session.execute(_ROWS_SQL, {"model_version": model_version}).fetchall()
    return [(r.chat_id, r.message_id) for r in rows]


def pending_understanding_count(session: Session, *, model_version: str) -> int:
    """Return the total number of pending messages at the given model_version."""
    return int(session.execute(_COUNT_SQL, {"model_version": model_version}).scalar_one())
