"""add radar_alerts.source_sent_at + backfill from supporting_message_ids

Revision ID: 0006_radar_alert_source_sent_at
Revises: 0005_commitment_source_sent_at
Create Date: 2026-04-29

The brief was filtering radar by alert.created_at, which is the worker's
clock. Backfilled understandings produced fresh-looking alerts that
referenced months-old conversations (e.g. Mieszko's 2025-09-08 'tonight'
signing request showed up as a today's brief item).

Add source_sent_at = MAX(messages.sent_at across supporting_message_ids).
Brief filters move to source_sent_at; aggregator populates it on every
new/updated alert. Old alerts whose supporting messages are now ancient
will fall out of the 24h brief window naturally.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_radar_alert_source_sent_at"
down_revision: str | None = "0005_commitment_source_sent_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "radar_alerts",
        sa.Column("source_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill: max sent_at across supporting_message_ids JSONB array.
    # Each element is {"chat_id": int, "message_id": int}.
    op.execute(
        """
        UPDATE radar_alerts ra
        SET source_sent_at = sub.max_sent_at
        FROM (
            SELECT
                ra2.id AS alert_id,
                MAX(m.sent_at) AS max_sent_at
            FROM radar_alerts ra2
            CROSS JOIN LATERAL jsonb_array_elements(ra2.supporting_message_ids) AS smi
            JOIN messages m
              ON m.chat_id = (smi->>'chat_id')::bigint
             AND m.message_id = (smi->>'message_id')::bigint
            WHERE ra2.supporting_message_ids IS NOT NULL
              AND jsonb_typeof(ra2.supporting_message_ids) = 'array'
            GROUP BY ra2.id
        ) sub
        WHERE ra.id = sub.alert_id
          AND ra.source_sent_at IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("radar_alerts", "source_sent_at")
