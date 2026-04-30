"""RouterDecision — the structured output that rules and (later) the LLM
classifier both produce. Executors consume it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Allowed intents. `qa` = falls through to Claude. `ambiguous` = ask user
# to rephrase. The rest are dispatched to local executors.
Intent = Literal[
    "feedback",
    "commitment_resolve",
    "commitment_cancel",
    "commitment_update",
    "qa",
    "retag",
    "ambiguous",
]

Source = Literal["rule", "llm"]


@dataclass
class RouterDecision:
    """Structured routing decision produced by rules.py or llm.py.

    Fields beyond `intent`/`confidence`/`source` are intent-specific and
    held in `fields`. Executors read them by key.

    Confidence semantics:
    - rule path: hardcoded 1.0 — regex matches are deterministic.
    - llm path (PR2): self-reported by Qwen, used as a soft signal only;
      structural validation is the real loop guard.
    """

    intent: Intent
    confidence: float
    source: Source
    fields: dict[str, Any] = field(default_factory=dict)
