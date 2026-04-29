"""add commitments.source_sent_at + backfill from messages.sent_at

Revision ID: 0005_commitment_source_sent_at
Revises: 0004_rename_colleague_tag
Create Date: 2026-04-29

The brief was computing commitment age from `created_at` (the extractor's
clock), which made backfilled commitments from months-old messages look
days-old. Add `source_sent_at` (the original message's send time) and
backfill all existing rows by joining to the `messages` table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_commitment_source_sent_at"
down_revision: str | None = "0004_rename_colleague_tag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "commitments",
        sa.Column("source_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill: copy messages.sent_at into source_sent_at for every commitment
    # whose source_message_id resolves to an existing message.
    op.execute(
        """
        UPDATE commitments c
        SET source_sent_at = m.sent_at
        FROM messages m
        WHERE c.chat_id = m.chat_id
          AND c.source_message_id = m.message_id
          AND c.source_sent_at IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("commitments", "source_sent_at")
