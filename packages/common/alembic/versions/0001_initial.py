"""initial schema — mirrors docs/mvp-spec.md §4

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "chats",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("username", sa.Text()),
        sa.Column("tag", sa.Text()),
        sa.Column("tag_set_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text()),
        sa.Column("participant_count", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "users",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("first_name", sa.Text()),
        sa.Column("last_name", sa.Text()),
        sa.Column("username", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.Column("is_self", sa.Boolean(), server_default=sa.false()),
    )

    op.create_table(
        "messages",
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("chats.chat_id"), nullable=False),
        sa.Column("sender_id", sa.BigInteger(), sa.ForeignKey("users.user_id")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("reply_to_id", sa.BigInteger()),
        sa.Column("edited_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("raw", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "message_id"),
    )
    op.create_index("ix_messages_sent_at", "messages", [sa.text("sent_at DESC")])
    op.execute(
        "CREATE INDEX ix_messages_text_tsv ON messages "
        "USING GIN (to_tsvector('simple', coalesce(text, '')))"
    )

    op.create_table(
        "message_understanding",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("language", sa.Text()),
        sa.Column("entities", JSONB()),
        sa.Column("intent", sa.Text()),
        sa.Column("is_directed_at_user", sa.Boolean()),
        sa.Column("is_commitment", sa.Boolean()),
        sa.Column("commitment", JSONB()),
        sa.Column("is_signal", sa.Boolean()),
        sa.Column("signal_type", sa.Text()),
        sa.Column("signal_strength", sa.SmallInteger()),
        sa.Column("sentiment_delta", sa.SmallInteger()),
        sa.Column("summary_en", sa.Text()),
        sa.Column("embedding", Vector(1024)),
        sa.PrimaryKeyConstraint("chat_id", "message_id"),
        sa.ForeignKeyConstraint(
            ["chat_id", "message_id"], ["messages.chat_id", "messages.message_id"]
        ),
    )
    op.execute(
        "CREATE INDEX ix_mu_embedding_hnsw ON message_understanding "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_mu_signal ON message_understanding "
        "(is_signal, signal_strength DESC) WHERE is_signal"
    )
    op.execute(
        "CREATE INDEX ix_mu_directed ON message_understanding "
        "(is_directed_at_user) WHERE is_directed_at_user"
    )

    op.create_table(
        "commitments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("source_message_id", sa.BigInteger()),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by_message_id", sa.BigInteger()),
        sa.Column("status", sa.Text(), server_default="open"),
    )

    op.create_table(
        "radar_alerts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("alert_type", sa.Text(), nullable=False),
        sa.Column("severity", sa.SmallInteger()),
        sa.Column("title", sa.Text()),
        sa.Column("reasoning", sa.Text()),
        sa.Column("supporting_message_ids", JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("surfaced_in_brief_at", sa.DateTime(timezone=True)),
        sa.Column("user_feedback", sa.Text()),
        sa.Column("feedback_note", sa.Text()),
    )

    op.create_table(
        "relationship_state",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("stage", sa.Text()),
        sa.Column("stage_confidence", sa.SmallInteger()),
        sa.Column("last_meaningful_contact_at", sa.DateTime(timezone=True)),
        sa.Column("last_user_message_at", sa.DateTime(timezone=True)),
        sa.Column("last_counterparty_message_at", sa.DateTime(timezone=True)),
        sa.Column("temperature", sa.Text()),
        sa.Column("open_threads", JSONB()),
        sa.Column("user_override", JSONB()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "chat_summaries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("key_points", JSONB()),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("chat_id", "period", "period_start"),
    )

    op.create_table(
        "brief_feedback",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("brief_date", sa.Date(), nullable=False),
        sa.Column("item_ref", sa.Text()),
        sa.Column("feedback", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("brief_feedback")
    op.drop_table("chat_summaries")
    op.drop_table("relationship_state")
    op.drop_table("radar_alerts")
    op.drop_table("commitments")
    op.drop_table("message_understanding")
    op.drop_table("messages")
    op.drop_table("users")
    op.drop_table("chats")
