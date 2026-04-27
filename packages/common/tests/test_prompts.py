"""Sanity checks on canonical prompt constants."""

from tbc_common.prompts import (
    BRIEF_SYSTEM,
    MODEL_VERSION,
    UNDERSTANDING_SYSTEM,
    WEEKLY_SYSTEM,
)


def test_prompts_are_non_empty() -> None:
    assert len(UNDERSTANDING_SYSTEM) > 500
    assert len(BRIEF_SYSTEM) > 500
    assert len(WEEKLY_SYSTEM) > 300


def test_model_version_is_set() -> None:
    assert MODEL_VERSION.startswith("understanding-")


def test_understanding_prompt_has_schema() -> None:
    # The understanding prompt must declare the JSON schema fields the
    # worker depends on.
    required = [
        "language",
        "entities",
        "intent",
        "is_directed_at_user",
        "is_commitment",
        "commitment",
        "is_signal",
        "signal_type",
        "signal_strength",
        "sentiment_delta",
        "summary_en",
    ]
    for field in required:
        assert field in UNDERSTANDING_SYSTEM, f"schema field {field!r} missing from prompt"


def test_brief_prompt_has_sections() -> None:
    for section in [
        "THE SHAPE OF TODAY",
        "ON YOUR PLATE",
        "WAITING ON OTHERS",
        "WORTH NOTICING",
        "TEMPERATURE CHECK",
        "IF YOU ONLY DO THREE THINGS",
    ]:
        assert section in BRIEF_SYSTEM
