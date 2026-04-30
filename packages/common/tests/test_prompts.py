"""Sanity checks on canonical prompt builders."""

from tbc_common.prompts import (
    MODEL_VERSION,
    build_brief_system,
    build_understanding_system,
    build_weekly_system,
)

# All prompt builders accept a list of Tag objects; tests use [] (the
# fallback path) since they assert structural properties of the static
# template, not the dynamic per-tag guidance block.
BRIEF_SYSTEM = build_brief_system([])
UNDERSTANDING_SYSTEM = build_understanding_system([])
WEEKLY_SYSTEM = build_weekly_system([])


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
        "IF YOU ONLY DO THREE THINGS",
    ]:
        assert section in BRIEF_SYSTEM


def test_brief_prompt_dropped_temperature_section() -> None:
    """TEMPERATURE CHECK was removed — relationship cooling/warming gets
    folded into WORTH NOTICING or ON YOUR PLATE instead. Regression
    catches an accidental re-introduction."""
    assert "TEMPERATURE CHECK" not in BRIEF_SYSTEM
    assert "🌡️" not in BRIEF_SYSTEM


def test_brief_prompt_instructs_short_id_preservation() -> None:
    """The brief LLM must be told to preserve `(c<id>)` commitment tags
    inline in ON YOUR PLATE / WAITING ON OTHERS so the user can mark
    them later. Without this, the rendered (c<id>) on input rows can
    silently get dropped from the output."""
    assert "(c<id>)" in BRIEF_SYSTEM
