"""Create tags table — dynamic tag registry with per-tag analysis guidance.

Revision ID: 0007_tags_table
Revises: 0006_radar_alert_source_sent_at
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_tags_table"
down_revision: str | None = "0006_radar_alert_source_sent_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tags",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("analysis_guidance", sa.Text, nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "is_system",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "sort_order",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.execute(
        """
        INSERT INTO tags (name, description, analysis_guidance, is_system, sort_order) VALUES
        (
            'client',
            'Existing paying customer; established business relationship',
            'Prioritise commitments, open threads, and relationship temperature. Flag cooling signals and upsell opportunities. A non-response from a client is more urgent than from a prospect.',
            TRUE, 1
        ),
        (
            'prospect',
            'Potential customer; pre-sale conversation in progress',
            'Watch for buying signals, objections, and timeline shifts. Track whether the prospect is progressing or stalling. Note competitor mentions.',
            TRUE, 2
        ),
        (
            'supplier',
            'Vendor the user buys from — procurement direction, not sales',
            'Read messages as procurement signals: delivery issues, pricing, quality. supplier_issue signals are high priority. Never frame supplier chats as sales opportunities.',
            TRUE, 3
        ),
        (
            'partner',
            'Joint-execution, co-marketing, agency, or referral partner',
            'Track partnership signals and milestone progress. Referral opportunities and co-execution threads are the priority. Partner is not a client — do not apply sales framing.',
            TRUE, 4
        ),
        (
            'internal',
            'Colleagues, employees, the user''s own team',
            'Internal commitments should be tracked like client ones. Flag cooling in team relationships and risk signals. Team coordination matters here.',
            TRUE, 5
        ),
        (
            'friend',
            'Personal friendship; non-family social connection',
            'Apply personal signals only: favor_request, relationship_drift, emotional_support, celebration. Never apply business framing — a friend mentioning a price is not a buying signal. Nudge style: soft check-in, never a sales follow-up.',
            TRUE, 6
        ),
        (
            'family',
            'Family members — relatives',
            'Highest personal weight. relationship_drift from family is especially important. Never apply business signals. Use warm, personal tone in suggestions.',
            TRUE, 7
        ),
        (
            'personal',
            'Personal context that is not friend or family (e.g. a doctor, accountant)',
            'Treat as personal: personal_event and favor_request signals apply. May have occasional business-adjacent content — read carefully and do not default to sales framing.',
            TRUE, 8
        ),
        (
            'ignore',
            'Bots, channels, group spam, transient or low-signal chats',
            'Exclude from all briefs and analysis. Do not surface any signals or items from ignored chats.',
            TRUE, 9
        );
        """
    )


def downgrade() -> None:
    op.drop_table("tags")
