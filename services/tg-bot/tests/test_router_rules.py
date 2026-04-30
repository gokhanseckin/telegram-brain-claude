"""Unit tests for the rule-based pre-router. Pure regex; no I/O."""

from __future__ import annotations

import pytest
from tbc_bot.router.rules import match_rule


@pytest.mark.parametrize(
    "text,expected_type,expected_ref,expected_note",
    [
        ("#abcd useful", "useful", "abcd", None),
        ("#ABcd useful", "useful", "abcd", None),  # ref lowercased
        ("#abcd not useful", "not_useful", "abcd", None),
        ("#abcd not_useful", "not_useful", "abcd", None),
        ("useful #abcd", "useful", "abcd", None),
        ("no #abcd", "not_useful", "abcd", None),
        ("#abcd noise", "not_useful", "abcd", None),
        ("#abcd irrelevant", "not_useful", "abcd", None),
        ("#abcd good", "useful", "abcd", None),
        ("#abcd missed", "missed_important", "abcd", None),
        # Note capture
        ('#abcd not_useful "just smalltalk"', "not_useful", "abcd", "just smalltalk"),
        ("#abcd useful makes sense", "useful", "abcd", "makes sense"),
        # Trailing period normalisation
        ("#abcd useful.", "useful", "abcd", None),
        # Mixed case sentiment
        ("#abcd Useful", "useful", "abcd", None),
        ("#ABCDEF12 useful", "useful", "abcdef12", None),  # 8-char hex
    ],
)
def test_clean_matches(text, expected_type, expected_ref, expected_note):
    decision = match_rule(text)
    assert decision is not None, f"expected match for {text!r}"
    assert decision.intent == "feedback"
    assert decision.confidence == 1.0
    assert decision.source == "rule"
    assert decision.fields["feedback_type"] == expected_type
    assert decision.fields["item_ref"] == expected_ref
    assert decision.fields["note"] == expected_note


@pytest.mark.parametrize(
    "text",
    [
        # Ambiguous: free-text reaction with a tag — needs LLM, must NOT
        # be guessed by rules. This is the user's real-world case from
        # Stage 1 verification.
        "#a8ce Doğa is not a prospect, he is a friend",
        "#a8ce interesting take",
        "#a8ce ok",
        "#a8ce meh",
        # No tag — would need LLM to extract topic
        "you missed the Acme thing",
        "the report you wrote was great",
        # Q&A
        "what did Alice say last week?",
        "did I commit to send the report?",
        # Commitment-shaped (handled in PR2 by Qwen)
        "done with the report",
        "I sent the report to Bob",
        # Empty / whitespace
        "",
        "   ",
        # Tag too short / too long
        "#abc useful",
        "#abcdefghi useful",
        # Sentiment word as bare text without a tag
        "useful",
        # Garbage prefix
        "lol #abcd useful",
    ],
)
def test_no_match(text):
    """Anything outside the strict-sentiment vocab must NOT be classified
    by rules. The downstream LLM/Claude path handles it."""
    assert match_rule(text) is None


@pytest.mark.parametrize(
    "text,expected_intent,expected_id,expected_note_or_reason",
    [
        ("done c42", "commitment_resolve", 42, None),
        ("done c42 sent today", "commitment_resolve", 42, "sent today"),
        ("DONE C42", "commitment_resolve", 42, None),
        ("finished c1", "commitment_resolve", 1, None),
        ("completed c9999 with extra context", "commitment_resolve", 9999, "with extra context"),
        ("resolved c7", "commitment_resolve", 7, None),
        ("cancel c42", "commitment_cancel", 42, None),
        ("cancel c42 no longer needed", "commitment_cancel", 42, "no longer needed"),
        ("cancelled c5", "commitment_cancel", 5, None),
        ("drop c12 overcome by events", "commitment_cancel", 12, "overcome by events"),
        ("forget c8", "commitment_cancel", 8, None),
    ],
)
def test_commitment_shortcut_rule(text, expected_intent, expected_id, expected_note_or_reason):
    decision = match_rule(text)
    assert decision is not None, f"expected match for {text!r}"
    assert decision.intent == expected_intent
    assert decision.confidence == 1.0
    assert decision.source == "rule"
    assert decision.fields["commitment_id"] == expected_id
    field = "note" if expected_intent == "commitment_resolve" else "reason"
    assert decision.fields.get(field) == expected_note_or_reason


@pytest.mark.parametrize(
    "text",
    [
        # No `c` prefix → leave to Claude (could be free-text "done with...")
        "done 42",
        # Free-text without explicit id — must NOT match the shortcut path
        "done with the report",
        "I sent the report",
        "cancel the contract thing",
        "forget about it",
        # Hex tag accidentally typed without #: don't conflate
        "done abcd",
    ],
)
def test_commitment_shortcut_requires_explicit_c_prefix(text):
    """Free-text commitment phrasing without `c<id>` falls through to Qwen/Claude.
    The point of the rule path is determinism — only act when the id is explicit."""
    decision = match_rule(text)
    if decision is not None:
        # If something matched, it must NOT be a commitment intent
        assert decision.intent not in ("commitment_resolve", "commitment_cancel")


@pytest.mark.parametrize(
    "text,expected_target,expected_tag",
    [
        ("#86ab personal", "86ab", "personal"),
        ("#ABCD client", "abcd", "client"),
        ("prospect #1234abcd", "1234abcd", "prospect"),
        ("#86ab internal", "86ab", "internal"),
        ("ignore #abcd", "abcd", "ignore"),
        ("#abcd friend", "abcd", "friend"),
        ("family #abcd", "abcd", "family"),
        ("supplier #abcd", "abcd", "supplier"),
        ("partner #abcd", "abcd", "partner"),
    ],
)
def test_retag_rule_match(text, expected_target, expected_tag):
    decision = match_rule(text)
    assert decision is not None, f"expected match for {text!r}"
    assert decision.intent == "retag"
    assert decision.confidence == 1.0
    assert decision.source == "rule"
    assert decision.fields["target"] == expected_target
    assert decision.fields["new_tag"] == expected_tag


@pytest.mark.parametrize(
    "text",
    [
        "Doğa personal",      # name-based, no hex ref — needs LLM
        "#abcd blah",          # "blah" not a valid tag
        "personal",            # bare tag word, no ref
        "#abcd",               # ref without tag
    ],
)
def test_retag_rule_no_match(text):
    decision = match_rule(text)
    if decision is not None:
        assert decision.intent != "retag"


def test_note_quotes_stripped():
    decision = match_rule('#abcd not_useful "duplicate of yesterday"')
    assert decision is not None
    assert decision.fields["note"] == "duplicate of yesterday"


def test_ref_uppercase_input_lowercased():
    decision = match_rule("#ABCD useful")
    assert decision is not None
    assert decision.fields["item_ref"] == "abcd"
