"""Pydantic return types for MCP tools."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel


class MessageResult(BaseModel):
    chat_id: int
    chat_title: str | None
    chat_tag: str | None
    message_id: int
    sent_at: datetime
    sender_name: str | None
    text: str | None
    summary_en: str | None
    signal_type: str | None
    url: str  # tg:// deep link


class ChatSummaryResult(BaseModel):
    id: int
    chat_id: int
    period: str
    period_start: date
    summary: str
    key_points: list[Any] | None
    generated_at: datetime

    # Extra fields populated from chats join for list_chats
    chat_title: str | None = None
    chat_tag: str | None = None
    last_activity: datetime | None = None
    participant_count: int | None = None


class CommitmentResult(BaseModel):
    id: int
    chat_id: int | None
    source_message_id: int | None
    owner: str
    description: str
    due_at: datetime | None
    created_at: datetime
    resolved_at: datetime | None
    resolved_by_message_id: int | None
    status: str


class SignalResult(BaseModel):
    chat_id: int
    message_id: int
    signal_type: str | None
    signal_strength: int | None
    summary_en: str | None
    processed_at: datetime
    # from messages join
    sent_at: datetime | None = None
    chat_title: str | None = None
    chat_tag: str | None = None


class RelationshipStateResult(BaseModel):
    chat_id: int
    stage: str | None
    stage_confidence: int | None
    last_meaningful_contact_at: datetime | None
    last_user_message_at: datetime | None
    last_counterparty_message_at: datetime | None
    temperature: str | None
    open_threads: list[dict[str, Any]] | None
    user_override: dict[str, Any] | None
    updated_at: datetime
    # from chats join
    chat_title: str | None = None
    chat_tag: str | None = None


class ChatListItem(BaseModel):
    chat_id: int
    title: str | None
    tag: str | None
    participant_count: int | None
    last_activity: datetime | None


class BriefText(BaseModel):
    date: date | None
    content: str
    generated_at: datetime | None
