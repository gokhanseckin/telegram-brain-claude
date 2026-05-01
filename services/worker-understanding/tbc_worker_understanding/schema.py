"""Pydantic schema for the Ollama understanding response."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class UnderstandingOutput(BaseModel):
    language: str = "other"
    entities: list[dict[str, str]] = []
    intent: str = "other"
    is_directed_at_user: bool = False
    is_commitment: bool = False
    commitment: dict[str, Any] | None = None
    is_signal: bool = False
    signal_type: str | None = None
    signal_strength: int | None = None
    sentiment_delta: int = 0
    summary_en: str = ""
