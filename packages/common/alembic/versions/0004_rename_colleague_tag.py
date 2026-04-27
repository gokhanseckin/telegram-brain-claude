"""rename Chat.tag 'colleague' to 'internal' to match new taxonomy

Revision ID: 0004_rename_colleague_tag
Revises: 0003_chat_tagging
Create Date: 2026-04-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_rename_colleague_tag"
down_revision: str | None = "0003_chat_tagging"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The new taxonomy uses 'internal' instead of 'colleague'. Update existing
    # manually-tagged chats in place. tag_locked is preserved.
    op.execute(
        "UPDATE chats SET tag = 'internal' WHERE tag = 'colleague'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE chats SET tag = 'colleague' "
        "WHERE tag = 'internal' AND tag_source = 'manual'"
    )
