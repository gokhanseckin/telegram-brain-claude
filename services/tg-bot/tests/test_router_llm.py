"""Unit tests for the LLM router classifier.

Tests are split into:
- _validate_decision: pure schema validation, no Ollama
- _parse_llm_json: tolerant parser for the noisy outputs Qwen sometimes
  produces (markdown fences, prepended sentences)
- classify(): the orchestrator, with a stubbed OllamaClient

The eval set is in test_router_eval.py — that's where prompt
regressions get caught.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from tbc_bot.router.llm import (
    _extract_json_object,
    _parse_llm_json,
    _validate_decision,
    classify,
)

# ---------- JSON parser tolerance ----------


def test_parse_clean_json():
    raw = '{"intent": "qa", "confidence": 0.9, "reason": "x"}'
    out = _parse_llm_json(raw)
    assert out == {"intent": "qa", "confidence": 0.9, "reason": "x"}


def test_parse_markdown_fenced():
    raw = '```json\n{"intent": "qa", "confidence": 0.9}\n```'
    out = _parse_llm_json(raw)
    assert out == {"intent": "qa", "confidence": 0.9}


def test_parse_with_prefix_text():
    raw = 'Sure, here is the JSON:\n{"intent": "qa", "confidence": 0.9}\nHope that helps!'
    out = _parse_llm_json(raw)
    assert out == {"intent": "qa", "confidence": 0.9}


def test_parse_garbage_returns_none():
    assert _parse_llm_json("not even close to json") is None
    assert _parse_llm_json("") is None
    assert _parse_llm_json("{") is None  # unbalanced


def test_extract_object_handles_nested_braces():
    raw = '{"a": {"b": 1}, "c": "}"}'
    extracted = _extract_json_object(raw)
    assert extracted is not None
    assert json.loads(extracted) == {"a": {"b": 1}, "c": "}"}


# ---------- Schema validation ----------


def _v(payload):
    return _validate_decision(payload)


def test_validate_feedback_with_ref():
    d = _v({
        "intent": "feedback", "confidence": 0.9, "reason": "x",
        "fields": {"feedback_type": "useful", "item_ref": "ab12"},
    })
    assert d is not None
    assert d.intent == "feedback"
    assert d.fields["feedback_type"] == "useful"
    assert d.fields["item_ref"] == "ab12"


def test_validate_feedback_strips_hash_in_ref():
    d = _v({
        "intent": "feedback", "confidence": 0.9,
        "fields": {"feedback_type": "not_useful", "item_ref": "#AB12"},
    })
    assert d is not None
    assert d.fields["item_ref"] == "ab12"


def test_validate_feedback_invalid_type_rejected():
    d = _v({
        "intent": "feedback", "confidence": 0.9,
        "fields": {"feedback_type": "great"},  # not in allowlist
    })
    assert d is None


def test_validate_feedback_invalid_ref_format_rejected():
    d = _v({
        "intent": "feedback", "confidence": 0.9,
        "fields": {"feedback_type": "useful", "item_ref": "not-hex!"},
    })
    assert d is None


def test_validate_feedback_missing_type_rejected():
    d = _v({
        "intent": "feedback", "confidence": 0.9, "fields": {"item_ref": "ab12"},
    })
    assert d is None


def test_validate_intent_outside_allowlist():
    d = _v({"intent": "smalltalk", "confidence": 0.9, "fields": {}})
    assert d is None


def test_validate_commitment_resolve_requires_query():
    d = _v({
        "intent": "commitment_resolve", "confidence": 0.9, "fields": {},
    })
    assert d is None
    d2 = _v({
        "intent": "commitment_resolve", "confidence": 0.9,
        "fields": {"query": "report Bob"},
    })
    assert d2 is not None
    assert d2.fields["query"] == "report Bob"


def test_validate_qa_no_required_fields():
    d = _v({"intent": "qa", "confidence": 0.95, "fields": {}})
    assert d is not None
    assert d.intent == "qa"


def test_validate_ambiguous_passes():
    d = _v({"intent": "ambiguous", "confidence": 0.3, "fields": {}})
    assert d is not None
    assert d.intent == "ambiguous"


def test_validate_confidence_clamped():
    d = _v({"intent": "qa", "confidence": 9.99, "fields": {}})
    assert d is not None
    assert d.confidence == 1.0
    d2 = _v({"intent": "qa", "confidence": -1, "fields": {}})
    assert d2 is not None
    assert d2.confidence == 0.0


def test_validate_retag_valid():
    d = _v({
        "intent": "retag", "confidence": 0.9,
        "fields": {"target": "Doğa", "new_tag": "personal"},
    })
    assert d is not None
    assert d.intent == "retag"
    assert d.fields["target"] == "Doğa"
    assert d.fields["new_tag"] == "personal"


def test_validate_retag_missing_target_rejected():
    d = _v({
        "intent": "retag", "confidence": 0.9,
        "fields": {"new_tag": "personal"},
    })
    assert d is None


def test_validate_retag_invalid_tag_rejected():
    d = _v({
        "intent": "retag", "confidence": 0.9,
        "fields": {"target": "Doğa", "new_tag": "notarole"},
    })
    assert d is None


def test_validate_retag_empty_target_rejected():
    d = _v({
        "intent": "retag", "confidence": 0.9,
        "fields": {"target": "   ", "new_tag": "personal"},
    })
    assert d is None


def test_validate_confidence_non_numeric_rejected():
    d = _v({"intent": "qa", "confidence": "high", "fields": {}})
    assert d is None


def test_validate_fields_must_be_dict():
    d = _v({"intent": "qa", "confidence": 0.9, "fields": "oops"})
    assert d is None


# ---------- classify() orchestrator ----------


def _stub_client(response: str | Exception) -> AsyncMock:
    client = AsyncMock()
    if isinstance(response, Exception):
        client.chat = AsyncMock(side_effect=response)
    else:
        client.chat = AsyncMock(return_value=response)
    return client


@pytest.fixture(autouse=True)
def patch_settings(monkeypatch):
    """Pin router_min_confidence to 0.7 so threshold tests are stable."""
    from tbc_common.config import settings
    monkeypatch.setattr(settings, "router_min_confidence", 0.7)
    monkeypatch.setattr(settings, "router_model", "qwen2.5:3b-test")
    yield


@pytest.mark.asyncio
async def test_classify_clean_feedback():
    client = _stub_client(
        '{"intent":"feedback","confidence":0.95,"reason":"x",'
        '"fields":{"feedback_type":"useful","item_ref":"ab12"}}'
    )
    decision = await classify("the #ab12 was useful", client=client)
    assert decision.intent == "feedback"
    assert decision.confidence == 0.95
    assert decision.source == "llm"
    assert decision.fields["feedback_type"] == "useful"


@pytest.mark.asyncio
async def test_classify_low_confidence_becomes_ambiguous():
    """Soft confidence threshold below 0.7 must collapse to ambiguous —
    NOT escalate to Claude. That's the loop guard."""
    client = _stub_client(
        '{"intent":"commitment_resolve","confidence":0.5,'
        '"fields":{"query":"vague hint"}}'
    )
    decision = await classify("done maybe", client=client)
    assert decision.intent == "ambiguous"
    assert decision.fields["error"] == "low_confidence"
    assert decision.fields["classified_intent"] == "commitment_resolve"


@pytest.mark.asyncio
async def test_classify_ambiguous_low_confidence_kept_as_ambiguous():
    """If model itself returns ambiguous at low confidence, that's still
    ambiguous — not double-flagged."""
    client = _stub_client(
        '{"intent":"ambiguous","confidence":0.3,"reason":"too short","fields":{}}'
    )
    decision = await classify("ok", client=client)
    assert decision.intent == "ambiguous"
    assert "low_confidence" not in decision.fields.get("error", "")


@pytest.mark.asyncio
async def test_classify_ollama_error_becomes_ambiguous():
    """Network / model failures must not silently escalate."""
    client = _stub_client(RuntimeError("ollama down"))
    decision = await classify("anything", client=client)
    assert decision.intent == "ambiguous"
    assert decision.fields["error"] == "llm_call_failed"


@pytest.mark.asyncio
async def test_classify_unparseable_response_becomes_ambiguous():
    client = _stub_client("I'm sorry I can't help with that")
    decision = await classify("anything", client=client)
    assert decision.intent == "ambiguous"
    assert decision.fields["error"] == "unparseable_json"


@pytest.mark.asyncio
async def test_classify_schema_mismatch_becomes_ambiguous():
    client = _stub_client('{"intent":"feedback","confidence":0.9,"fields":{}}')
    decision = await classify("anything", client=client)
    assert decision.intent == "ambiguous"
    assert decision.fields["error"] == "schema_mismatch"


@pytest.mark.asyncio
async def test_classify_qa_passes_through():
    client = _stub_client('{"intent":"qa","confidence":0.95,"reason":"x","fields":{}}')
    decision = await classify("what did Alice say last week?", client=client)
    assert decision.intent == "qa"


@pytest.mark.asyncio
async def test_classify_commitment_resolve():
    client = _stub_client(
        '{"intent":"commitment_resolve","confidence":0.9,'
        '"fields":{"query":"report Bob"}}'
    )
    decision = await classify("done with the report to Bob", client=client)
    assert decision.intent == "commitment_resolve"
    assert decision.fields["query"] == "report Bob"


@pytest.mark.asyncio
async def test_classify_passes_format_json_to_ollama():
    """Belt-and-suspenders: the LLM call must request native JSON mode."""
    client = _stub_client('{"intent":"qa","confidence":0.9,"fields":{}}')
    await classify("test", client=client)
    call_kwargs = client.chat.call_args.kwargs
    assert call_kwargs.get("format") == "json"
