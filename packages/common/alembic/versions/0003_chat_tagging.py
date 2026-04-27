"""chat tagging columns for auto-tagger

Revision ID: 0003_chat_tagging
Revises: 0002_service_state
Create Date: 2026-04-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_chat_tagging"
down_revision: str | None = "0002_service_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chats", sa.Column("tag_confidence", sa.Float(), nullable=True))
    op.add_column("chats", sa.Column("tag_source", sa.Text(), nullable=True))
    op.add_column(
        "chats",
        sa.Column(
            "tag_locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column("chats", sa.Column("tag_reason", sa.Text(), nullable=True))

    # Mark every existing tagged chat as 'manual' so the auto-tagger never
    # overwrites a hand-set tag from the bot.
    op.execute(
        "UPDATE chats SET tag_source = 'manual', tag_locked = TRUE "
        "WHERE tag IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("chats", "tag_reason")
    op.drop_column("chats", "tag_locked")
    op.drop_column("chats", "tag_source")
    op.drop_column("chats", "tag_confidence")
