"""DM router — local-first dispatch for free-text messages.

Stage 2 PR1: rules-only pre-router. Catches obvious feedback DMs
(`#xxxx useful` etc.) and dispatches them to the local executor without
going through Claude. Anything the rules don't match falls through to
the existing Claude agent path.

PR2 will add a Qwen 3B classifier between the rules and the Claude
fall-through, plus echo-back state for destructive intents
(commitment_resolve/cancel/update).
"""

from __future__ import annotations

from .decision import RouterDecision
from .rules import match_rule

__all__ = ["RouterDecision", "match_rule"]
