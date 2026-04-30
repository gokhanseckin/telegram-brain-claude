"""LLM-based intent classifier for the DM router.

Calls a small local model (Qwen 2.5 3B by default) via Ollama with
strict JSON output, parses the response, and applies the loop guard:
schema validation + soft confidence threshold. Anything outside the
intent allowlist or below the threshold becomes RouterDecision(
intent='ambiguous', ...) — which the chat handler turns into a
"please rephrase" reply WITHOUT calling Claude. That's how we
guarantee Qwen failures don't silently escalate.

Per-DM cost shape:
  rules match  →  0 LLM calls, 0 Claude calls
  rules miss   →  1 LLM call (this module), then:
    intent=feedback     → 0 Claude calls
    intent=qa or commitment_*
                        → 1 Claude call (in chat.py)
    intent=ambiguous    → 0 Claude calls
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog
from tbc_common.config import settings
from tbc_common.ollama import OllamaClient

from .decision import RouterDecision
from .prompt import ROUTER_SYSTEM_PROMPT

log = structlog.get_logger(__name__)

ALLOWED_INTENTS = {
    "feedback",
    "commitment_resolve",
    "commitment_cancel",
    "commitment_update",
    "qa",
    "ambiguous",
}
ALLOWED_FEEDBACK_TYPES = {"useful", "not_useful", "missed_important"}


def _extract_json_object(raw: str) -> str | None:
    """Pull the first balanced {...} object out of a possibly-noisy string.

    Lifted from worker-chat-tagger/classifier.py — same pattern, since
    Qwen sometimes wraps JSON in markdown fences or prepends a sentence
    despite "Return ONLY this JSON" instructions.
    """
    s = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
    return None


def _parse_llm_json(raw: str) -> dict[str, Any] | None:
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        extracted = _extract_json_object(raw)
        if extracted is None:
            return None
        try:
            return json.loads(extracted)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            return None


def _validate_decision(parsed: dict[str, Any]) -> RouterDecision | None:
    """Apply the schema half of the loop guard.

    Returns None if any structural check fails — the caller folds None
    into an `ambiguous` decision. Returns a typed RouterDecision when
    everything checks out (intent in allowlist, fields well-typed,
    intent-specific required fields present).
    """
    intent = parsed.get("intent")
    if intent not in ALLOWED_INTENTS:
        return None

    raw_conf = parsed.get("confidence", 0.0)
    try:
        confidence = float(raw_conf)
    except (TypeError, ValueError):
        return None
    confidence = max(0.0, min(1.0, confidence))

    fields_in = parsed.get("fields") or {}
    if not isinstance(fields_in, dict):
        return None

    fields_out: dict[str, Any] = {}

    if intent == "feedback":
        ft = fields_in.get("feedback_type")
        if ft not in ALLOWED_FEEDBACK_TYPES:
            return None
        fields_out["feedback_type"] = ft
        ref = fields_in.get("item_ref")
        if ref is not None:
            if not isinstance(ref, str):
                return None
            ref_clean = ref.strip().lstrip("#").lower()
            if ref_clean and not re.fullmatch(r"[0-9a-f]{4,8}", ref_clean):
                return None
            fields_out["item_ref"] = ref_clean or None
        else:
            fields_out["item_ref"] = None
        note = fields_in.get("note")
        if note is not None and not isinstance(note, str):
            return None
        fields_out["note"] = (note.strip() or None) if isinstance(note, str) else None

    elif intent in ("commitment_resolve", "commitment_cancel", "commitment_update"):
        query = fields_in.get("query")
        if query is None or not isinstance(query, str) or not query.strip():
            return None
        fields_out["query"] = query.strip()
        # commitment_update may carry due_at / note_append; stash if present
        for key in ("due_at", "note_append"):
            val = fields_in.get(key)
            if val is not None:
                if not isinstance(val, str):
                    return None
                fields_out[key] = val.strip()

    # qa and ambiguous have no required per-intent fields.

    return RouterDecision(
        intent=intent,  # validated against allowlist above
        confidence=confidence,
        source="llm",
        fields=fields_out,
    )


async def classify(text: str, client: OllamaClient | None = None) -> RouterDecision:
    """Classify a user DM via the local LLM.

    Always returns a RouterDecision. Failure modes (Ollama error,
    unparseable JSON, schema mismatch, low confidence) all collapse to
    `RouterDecision(intent='ambiguous', confidence=0.0, source='llm')`
    so the caller has one shape to dispatch on.

    `client` is injectable for tests; production callers leave it None.
    """
    cli = client if client is not None else OllamaClient(settings.ollama_base_url)

    try:
        raw = await cli.chat(
            model=settings.router_model,
            system=ROUTER_SYSTEM_PROMPT,
            user=text,
            format="json",
        )
    except Exception:
        log.exception("router_llm_call_failed")
        return RouterDecision(
            intent="ambiguous", confidence=0.0, source="llm",
            fields={"error": "llm_call_failed"},
        )

    parsed = _parse_llm_json(raw)
    if parsed is None:
        log.warning("router_llm_unparseable_json", raw=raw[:500])
        return RouterDecision(
            intent="ambiguous", confidence=0.0, source="llm",
            fields={"error": "unparseable_json"},
        )

    decision = _validate_decision(parsed)
    if decision is None:
        log.warning(
            "router_llm_schema_mismatch",
            intent=parsed.get("intent"),
            raw=raw[:500],
        )
        return RouterDecision(
            intent="ambiguous", confidence=0.0, source="llm",
            fields={"error": "schema_mismatch"},
        )

    # Soft confidence threshold — last gate before dispatch.
    if (
        decision.intent != "ambiguous"
        and decision.confidence < settings.router_min_confidence
    ):
        log.info(
            "router_llm_low_confidence",
            classified_intent=decision.intent,
            confidence=decision.confidence,
            threshold=settings.router_min_confidence,
        )
        return RouterDecision(
            intent="ambiguous",
            confidence=decision.confidence,
            source="llm",
            fields={"error": "low_confidence", "classified_intent": decision.intent},
        )

    return decision
