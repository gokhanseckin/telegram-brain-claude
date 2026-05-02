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
    catches an accidental re-introduction. Also: the brief must never
    use the CRM-speak word "temperature" in its output, only as a
    negative instruction telling the LLM to avoid it."""
    assert "TEMPERATURE CHECK" not in BRIEF_SYSTEM
    assert "🌡️" not in BRIEF_SYSTEM
    # The only allowed mention is the explicit instruction not to use it.
    assert BRIEF_SYSTEM.count("temperature") <= 1


def test_brief_prompt_preserves_worth_noticing_ref_tags() -> None:
    """Each WORTH NOTICING bullet sourced from a radar alert must end
    with its (#xxxx) tag so the user can DM `#xxxx useful` to rate it.
    Regression for the user-reported disappearance of these tags."""
    assert "(#xxxx)" in BRIEF_SYSTEM or "#xxxx" in BRIEF_SYSTEM
    assert "ref=#xxxx" in BRIEF_SYSTEM


def test_brief_prompt_has_quiet_period_rule() -> None:
    """Friend/family/personal chats should not get reach-out nudges
    unless they've been quiet for 7+ days."""
    assert "7+ days" in BRIEF_SYSTEM or "7 days" in BRIEF_SYSTEM


def test_brief_prompt_instructs_short_id_preservation() -> None:
    """The brief LLM must be told to preserve `(c<id>)` commitment tags
    in ON YOUR PLATE / WAITING ON OTHERS so the user can mark them
    later. Without this, the rendered (c<id>) on input rows can
    silently get dropped from the output.

    Also enforces one-commitment-per-bullet: if a person has multiple
    open commitments, they must get multiple bullets each ending with
    their own (c<id>), not a single grouped bullet trailing
    `(c1) (c2)` — that batched form breaks `done c<id>` resolution.
    """
    assert "(c<id>)" in BRIEF_SYSTEM
    # Normalize whitespace so wrapped phrases still match.
    flat = " ".join(BRIEF_SYSTEM.split())
    assert "One commitment per bullet" in flat
    assert "one tag per bullet" in flat
    assert "one bullet per commitment" in flat
