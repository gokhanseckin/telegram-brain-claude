"""service_state table for one-time lifecycle flags

Revision ID: 0002_service_state
Revises: 0001_initial
Create Date: 2026-04-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_service_state"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("initial_backfill_started_at", sa.DateTime(timezone=True)),
        sa.Column("initial_backfill_done_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("service_state")
