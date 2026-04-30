"""Drop client/friend/supplier tags; rename internal -> colleague.

User decision: scope down the tag taxonomy. The three removed tags lose their
chat assignments (rows become untagged and re-eligible for the chat-tagger).
`internal` is renamed to `colleague` everywhere — both the registry row and
the foreign-key-by-name on `chats.tag`.

Revision ID: 0008_remove_client_friend_supplier_rename_internal
Revises: 0007_tags_table
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_remove_client_friend_supplier_rename_internal"
down_revision: str | None = "0007_tags_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Clear chat assignments for tags being removed.
    op.execute(
        """
        UPDATE chats
        SET tag = NULL,
            tag_confidence = NULL,
            tag_source = NULL,
            tag_set_at = NULL,
            tag_reason = NULL,
            tag_locked = false
        WHERE tag IN ('client', 'friend', 'supplier')
        """
    )

    # Rename internal -> colleague on chats.
    op.execute("UPDATE chats SET tag = 'colleague' WHERE tag = 'internal'")

    # Rename the registry row, refreshing the description/guidance to match.
    op.execute(
        """
        UPDATE tags
        SET name = 'colleague',
            description = 'Colleagues, employees, the user''s own team',
            analysis_guidance = 'Colleague commitments are tracked like priority work threads. Flag cooling in team relationships and risk signals. Team coordination matters here.'
        WHERE name = 'internal'
        """
    )

    # Drop the removed registry rows.
    op.execute("DELETE FROM tags WHERE name IN ('client', 'friend', 'supplier')")


def downgrade() -> None:
    # Recreate the removed registry rows. Original analysis_guidance restored
    # from 0007_tags_table.
    op.execute(
        """
        INSERT INTO tags (name, description, analysis_guidance, is_system, is_active, sort_order)
        VALUES (
            'client',
            'Existing paying customer; established business relationship',
            'Prioritise commitments, open threads, and relationship temperature. Flag cooling signals and upsell opportunities. A non-response from a client is more urgent than from a prospect.',
            true,
            true,
            1
        ),
        (
            'supplier',
            'Vendor the user buys from — procurement direction, not sales',
            'Read messages as procurement signals: delivery issues, pricing, quality. supplier_issue signals are high priority. Never frame supplier chats as sales opportunities.',
            true,
            true,
            3
        ),
        (
            'friend',
            'Personal friendship; non-family social connection',
            'Apply personal signals only: favor_request, relationship_drift, emotional_support, celebration. Never apply business framing — a friend mentioning a price is not a buying signal. Nudge style: soft check-in, never a sales follow-up.',
            true,
            true,
            6
        )
        """
    )

    # Reverse the rename.
    op.execute("UPDATE chats SET tag = 'internal' WHERE tag = 'colleague'")
    op.execute(
        """
        UPDATE tags
        SET name = 'internal',
            description = 'Colleagues, employees, the user''s own team',
            analysis_guidance = 'Internal commitments should be tracked like client ones. Flag cooling in team relationships and risk signals. Team coordination matters here.'
        WHERE name = 'colleague'
        """
    )

    # NOTE: chat assignments cleared in upgrade() are not restored — those rows
    # will need to be re-tagged manually or by the chat-tagger.
