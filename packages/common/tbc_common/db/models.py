"""SQLAlchemy models for Telegram Business Brain.

Canonical translation of docs/mvp-spec.md §4. Field comments there apply here.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Chat(Base):
    __tablename__ = "chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    username: Mapped[str | None] = mapped_column(Text)
    tag: Mapped[str | None] = mapped_column(Text)  # client|prospect|supplier|partner|internal|friend|family|personal|ignore|NULL
    tag_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tag_confidence: Mapped[float | None] = mapped_column(Float)
    tag_source: Mapped[str | None] = mapped_column(Text)  # 'manual'|'auto_embedding'|'auto_llm'
    tag_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    tag_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    participant_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    username: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    is_self: Mapped[bool] = mapped_column(Boolean, default=False)


class Message(Base):
    __tablename__ = "messages"

    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.chat_id"), nullable=False
    )
    sender_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.user_id"))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    reply_to_id: Mapped[int | None] = mapped_column(BigInteger)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("chat_id", "message_id"),
        Index("ix_messages_sent_at", "sent_at"),
        # GIN text index is created in the Alembic migration (SQLAlchemy
        # lacks first-class `to_tsvector` index declaration).
    )


class MessageUnderstanding(Base):
    __tablename__ = "message_understanding"

    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    intent: Mapped[str | None] = mapped_column(Text)
    is_directed_at_user: Mapped[bool | None] = mapped_column(Boolean)
    is_commitment: Mapped[bool | None] = mapped_column(Boolean)
    commitment: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    is_signal: Mapped[bool | None] = mapped_column(Boolean)
    signal_type: Mapped[str | None] = mapped_column(Text)
    signal_strength: Mapped[int | None] = mapped_column(SmallInteger)
    sentiment_delta: Mapped[int | None] = mapped_column(SmallInteger)
    summary_en: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024))

    __table_args__ = (
        PrimaryKeyConstraint("chat_id", "message_id"),
        ForeignKeyConstraint(
            ["chat_id", "message_id"],
            ["messages.chat_id", "messages.message_id"],
        ),
        # HNSW vector index + partial indexes declared in Alembic migration.
    )


class Commitment(Base):
    __tablename__ = "commitments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    owner: Mapped[str] = mapped_column(Text, nullable=False)  # 'user' | 'counterparty'
    description: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # When the underlying conversation actually happened. created_at is when
    # the extractor wrote the row, which can be months later if the worker
    # backfilled historical messages. Use source_sent_at for true age and
    # recency filters; created_at remains for audit/debug.
    source_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, default="open")


class RadarAlert(Base):
    __tablename__ = "radar_alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    alert_type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[int | None] = mapped_column(SmallInteger)
    title: Mapped[str | None] = mapped_column(Text)
    reasoning: Mapped[str | None] = mapped_column(Text)
    supporting_message_ids: Mapped[list[dict[str, int]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Freshness anchor: max(sent_at) across supporting_message_ids. created_at
    # is when the aggregator wrote the row, which can be months after the
    # underlying conversation if the understanding worker is backfilling. Brief
    # filters use source_sent_at to avoid surfacing ancient signals as today's.
    source_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    surfaced_in_brief_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_feedback: Mapped[str | None] = mapped_column(Text)
    feedback_note: Mapped[str | None] = mapped_column(Text)


class RelationshipState(Base):
    __tablename__ = "relationship_state"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stage: Mapped[str | None] = mapped_column(Text)
    stage_confidence: Mapped[int | None] = mapped_column(SmallInteger)
    last_meaningful_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_user_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_counterparty_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    temperature: Mapped[str | None] = mapped_column(Text)
    open_threads: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    user_override: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatSummary(Base):
    __tablename__ = "chat_summaries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period: Mapped[str] = mapped_column(Text, nullable=False)  # 'day' | 'week'
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    key_points: Mapped[list[Any] | None] = mapped_column(JSONB)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("chat_id", "period", "period_start"),)


class ServiceState(Base):
    """Single-row table tracking one-time service lifecycle events."""

    __tablename__ = "service_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    initial_backfill_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    initial_backfill_done_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


# Authoritative set for the brief_feedback.feedback column. Imported by
# both the /feedback slash handler (tg-bot) and the write_brief_feedback
# MCP tool so they validate against the same truth. Adding a value here
# is the only place it should be added.
ALLOWED_FEEDBACK_TYPES: tuple[str, ...] = (
    "useful",
    "not_useful",
    "missed_important",
)


class BriefFeedback(Base):
    __tablename__ = "brief_feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    brief_date: Mapped[date] = mapped_column(Date, nullable=False)
    item_ref: Mapped[str | None] = mapped_column(Text)  # alert id or commitment id
    feedback: Mapped[str] = mapped_column(Text, nullable=False)  # see ALLOWED_FEEDBACK_TYPES above
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
