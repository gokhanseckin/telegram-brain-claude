# ruff: noqa: RUF001
# Turkish dotless-i and friends are intentional in this eval set.
"""Eval set for the LLM router prompt.

This test does NOT call a real Ollama. It stubs the Ollama client to
return *what we expect Qwen 3B to produce* given the prompt. The point
is to exercise the parser + validator + dispatch logic end-to-end on a
realistic spread of DMs.

When the real model regresses (e.g. classifies a Q&A as feedback), this
file is also where you'd add the failing case so the prompt change that
fixes it can be reviewed against the rest of the set.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from tbc_bot.router.llm import classify

# (input DM, canonical Qwen-style JSON output, expected_intent, key field)
EVAL: list[tuple[str, dict, str, dict | None]] = [
    # --- Feedback: explicit sentiment + tag ---
    (
        "the #ab12 was useful",
        {"intent": "feedback", "confidence": 0.95,
         "fields": {"feedback_type": "useful", "item_ref": "ab12"}},
        "feedback",
        {"feedback_type": "useful", "item_ref": "ab12"},
    ),
    # --- Feedback: explicit not_useful with note (the kind of free-text
    # reaction the Doğa example USED to claim — but actual chat-tag
    # corrections like "Doğa is personal" are now classified as
    # ambiguous; see the ambiguous section below).
    (
        "#a8ce not useful, just smalltalk",
        {"intent": "feedback", "confidence": 0.9,
         "fields": {"feedback_type": "not_useful", "item_ref": "a8ce",
                    "note": "just smalltalk"}},
        "feedback",
        {"feedback_type": "not_useful", "item_ref": "a8ce"},
    ),
    # --- Retag: chat-tag corrections ---
    (
        "Doğa is personal",
        {"intent": "retag", "confidence": 0.9,
         "fields": {"target": "Doğa", "new_tag": "personal"}},
        "retag",
        {"target": "Doğa", "new_tag": "personal"},
    ),
    (
        "#86ab personal",
        {"intent": "retag", "confidence": 0.95,
         "fields": {"target": "86ab", "new_tag": "personal"}},
        "retag",
        {"target": "86ab", "new_tag": "personal"},
    ),
    (
        "#a8ce Doğa is personal, not internal",
        {"intent": "retag", "confidence": 0.85,
         "fields": {"target": "a8ce", "new_tag": "personal"}},
        "retag",
        {"target": "a8ce", "new_tag": "personal"},
    ),
    # --- Ambiguous retag: unclear target ---
    (
        "that chat should be different",
        {"intent": "ambiguous", "confidence": 0.25, "fields": {}},
        "ambiguous",
        None,
    ),
    # --- Feedback: missed without tag ---
    (
        "you missed the Acme thing",
        {"intent": "feedback", "confidence": 0.85,
         "fields": {"feedback_type": "missed_important",
                    "note": "missed the Acme thing"}},
        "feedback",
        {"feedback_type": "missed_important"},
    ),
    # --- Commitment resolve ---
    (
        "done with the report to Bob",
        {"intent": "commitment_resolve", "confidence": 0.9,
         "fields": {"query": "report Bob"}},
        "commitment_resolve",
        {"query": "report Bob"},
    ),
    (
        "I paid Gizem the 67 euros",
        {"intent": "commitment_resolve", "confidence": 0.9,
         "fields": {"query": "Gizem 67"}},
        "commitment_resolve",
        None,
    ),
    # --- Commitment cancel ---
    (
        "forget the Acme contract thing",
        {"intent": "commitment_cancel", "confidence": 0.85,
         "fields": {"query": "Acme contract"}},
        "commitment_cancel",
        None,
    ),
    # --- Commitment update ---
    (
        "push the contract review to next Friday",
        {"intent": "commitment_update", "confidence": 0.85,
         "fields": {"query": "contract review"}},
        "commitment_update",
        None,
    ),
    # --- Q&A ---
    (
        "what did Alice say about pricing last week?",
        {"intent": "qa", "confidence": 0.95, "fields": {}},
        "qa",
        None,
    ),
    (
        "did I commit to send the report?",
        {"intent": "qa", "confidence": 0.9, "fields": {}},
        "qa",
        None,
    ),
    # --- Turkish: missed_important ---
    (
        "Berkay'ın #5096 mesajı önemliydi, brief'e koymalıydın",
        {"intent": "feedback", "confidence": 0.85,
         "fields": {"feedback_type": "missed_important", "item_ref": "5096",
                    "note": "Berkay'ın mesajı önemliydi"}},
        "feedback",
        {"feedback_type": "missed_important", "item_ref": "5096"},
    ),
    # --- Turkish: commitment resolve ---
    (
        "raporu gönderdim Bob'a",
        {"intent": "commitment_resolve", "confidence": 0.85,
         "fields": {"query": "rapor Bob"}},
        "commitment_resolve",
        None,
    ),
    # --- Ambiguous: no referent ---
    (
        "forget about it",
        {"intent": "ambiguous", "confidence": 0.3,
         "fields": {}},
        "ambiguous",
        None,
    ),
    # --- Ambiguous: multiple actions ---
    (
        "I sent the report and also paid Bob",
        {"intent": "ambiguous", "confidence": 0.4, "fields": {}},
        "ambiguous",
        None,
    ),
    # --- Ambiguous: too short ---
    (
        "ok",
        {"intent": "ambiguous", "confidence": 0.2, "fields": {}},
        "ambiguous",
        None,
    ),
]


@pytest.fixture(autouse=True)
def patch_settings(monkeypatch):
    from tbc_common.config import settings
    monkeypatch.setattr(settings, "router_min_confidence", 0.7)
    monkeypatch.setattr(settings, "router_model", "qwen2.5:3b-test")


@pytest.mark.parametrize("dm_text,canonical_json,expected_intent,key_fields", EVAL)
@pytest.mark.asyncio
async def test_eval_case(dm_text, canonical_json, expected_intent, key_fields):
    client = AsyncMock()
    client.chat = AsyncMock(return_value=json.dumps(canonical_json))

    decision = await classify(dm_text, client=client)

    # Ambiguous low-confidence is kept as ambiguous, but for feedback /
    # commitment / qa cases the soft threshold also matters.
    if expected_intent == "ambiguous":
        assert decision.intent == "ambiguous"
    else:
        assert decision.intent == expected_intent, (
            f"DM={dm_text!r} got {decision.intent}, want {expected_intent}; "
            f"fields={decision.fields}"
        )

    if key_fields:
        for k, v in key_fields.items():
            assert decision.fields.get(k) == v, (
                f"DM={dm_text!r} field {k}: got {decision.fields.get(k)!r}, want {v!r}"
            )


def test_eval_set_size_floor():
    """Don't let the eval set silently shrink. Stage 2 plan asked for
    25-30 entries; this is a budget marker."""
    # Slightly below the plan's 25 because we deferred commitment
    # executor coverage to PR3 — bump this in PR3.
    assert len(EVAL) >= 17
