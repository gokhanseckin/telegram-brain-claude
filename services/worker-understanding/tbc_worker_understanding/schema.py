"""Pydantic schema for the Ollama understanding response."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class UnderstandingOutput(BaseModel):
    language: str
    entities: list[dict[str, str]] = []
    intent: str
    is_directed_at_user: bool
    is_commitment: bool
    commitment: dict[str, Any] | None = None
    is_signal: bool
    signal_type: str | None = None
    signal_strength: int | None = None
    sentiment_delta: int
    summary_en: str
