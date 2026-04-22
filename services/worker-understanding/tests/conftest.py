"""Shared pytest fixtures for worker-understanding tests.

Uses an in-memory SQLite database for unit tests. Only the tables needed
by the understanding worker are created, with PostgreSQL-specific types
replaced by SQLite-compatible equivalents.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    JSON,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    create_engine,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


# Minimal SQLite-compatible reproductions of only the tables we need.

class Chat(Base):
    __tablename__ = "chats"
    chat_id = Column(BigInteger, primary_key=True)
    type = Column(Text, nullable=False, default="private")
    title = Column(Text)
    username = Column(Text)
    tag = Column(Text)
    tag_set_at = Column(DateTime)
    notes = Column(Text)
    participant_count = Column(Integer)
    created_at = Column(DateTime, default=func.now())


class Message(Base):
    __tablename__ = "messages"
    message_id = Column(BigInteger, nullable=False)
    chat_id = Column(BigInteger, ForeignKey("chats.chat_id"), nullable=False)
    sender_id = Column(BigInteger)
    sent_at = Column(DateTime, nullable=False)
    text = Column(Text)
    reply_to_id = Column(BigInteger)
    edited_at = Column(DateTime)
    deleted_at = Column(DateTime)
    raw = Column(JSON, nullable=False)

    __table_args__ = (PrimaryKeyConstraint("chat_id", "message_id"),)


class MessageUnderstanding(Base):
    __tablename__ = "message_understanding"
    chat_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    processed_at = Column(DateTime, default=func.now())
    model_version = Column(Text, nullable=False)
    language = Column(Text)
    entities = Column(JSON)
    intent = Column(Text)
    is_directed_at_user = Column(Boolean)
    is_commitment = Column(Boolean)
    commitment = Column(JSON)
    is_signal = Column(Boolean)
    signal_type = Column(Text)
    signal_strength = Column(SmallInteger)
    sentiment_delta = Column(SmallInteger)
    summary_en = Column(Text)
    embedding = Column(Text)  # JSON-serialised vector (SQLite has no pgvector)

    __table_args__ = (
        PrimaryKeyConstraint("chat_id", "message_id"),
        ForeignKeyConstraint(
            ["chat_id", "message_id"],
            ["messages.chat_id", "messages.message_id"],
        ),
    )


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    """Provide a transactional test session backed by local SQLite models."""
    SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with SessionFactory() as session:
        yield session


@pytest.fixture
def sample_chat(db_session: Session) -> Chat:
    chat = Chat(chat_id=1001, type="private", title="Test Client", tag="client")
    db_session.add(chat)
    db_session.commit()
    return chat


@pytest.fixture
def sample_message(db_session: Session, sample_chat: Chat) -> Message:
    msg = Message(
        chat_id=sample_chat.chat_id,
        message_id=1,
        sent_at=datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
        text="Let's schedule a demo next week.",
        raw={},
    )
    db_session.add(msg)
    db_session.commit()
    return msg
