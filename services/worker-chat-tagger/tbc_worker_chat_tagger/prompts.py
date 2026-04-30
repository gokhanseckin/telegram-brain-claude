"""Stage B: few-shot LLM prompt builder for chat tagging."""

from __future__ import annotations

from tbc_common.db.models import Tag
from tbc_common.db.tags import render_tag_definitions


def build_tagger_system_prompt(tags: list[Tag]) -> str:
    tag_block = render_tag_definitions(tags)
    return f"""\
You classify Telegram chats into roles. The user lives a connected life:
business deals, suppliers, partners, internal team, friends, family. Pick
the single tag that best describes the relationship in this chat.

{tag_block}

Decide based on language style, message subjects, who initiates, money
direction, and tone. Return ONLY this JSON, no prose:
{{"tag": "<one of the above>", "confidence": 0.0-1.0, "reason": "one sentence"}}
"""


def render_examples(examples: dict[str, list[list[str]]]) -> str:
    """Render few-shot examples grouped by tag.

    `examples` maps tag → list of chat samples, where each chat sample is a
    list of short message strings.
    """
    if not examples:
        return ""
    parts = ["Examples (existing tagged chats):", ""]
    for tag, chat_samples in examples.items():
        for i, msgs in enumerate(chat_samples, start=1):
            parts.append(f"--- Example {tag} #{i} ---")
            parts.extend(f"  • {m}" for m in msgs[:6])
            parts.append("")
    return "\n".join(parts)


def render_target(chat_title: str, messages: list[str]) -> str:
    """Render the chat we want to classify."""
    parts = [f"Classify this chat (title: {chat_title}):", ""]
    parts.extend(f"  • {m}" for m in messages)
    return "\n".join(parts)


def build_user_prompt(
    chat_title: str,
    target_messages: list[str],
    examples: dict[str, list[list[str]]] | None = None,
) -> str:
    blocks: list[str] = []
    if examples:
        blocks.append(render_examples(examples))
    blocks.append(render_target(chat_title, target_messages))
    blocks.append('Return ONLY the JSON object: {"tag": "...", "confidence": 0.0-1.0, "reason": "..."}')
    return "\n\n".join(blocks)
