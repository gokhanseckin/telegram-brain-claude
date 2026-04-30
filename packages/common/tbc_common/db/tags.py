"""Tag registry helpers — single source of truth for all services."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from tbc_common.db.models import Tag


def get_active_tags(session: Session) -> list[Tag]:
    return list(
        session.execute(
            select(Tag).where(Tag.is_active.is_(True)).order_by(Tag.sort_order)
        )
        .scalars()
        .all()
    )


def get_valid_tag_names(session: Session) -> set[str]:
    return {t.name for t in get_active_tags(session)}


def render_tag_definitions(tags: list[Tag]) -> str:
    lines = ["Tags:"]
    for tag in tags:
        lines.append(f"- {tag.name}: {tag.description}")
    return "\n".join(lines)


def render_tag_guidance(tags: list[Tag]) -> str:
    lines = ["## Per-tag analysis guidance"]
    for tag in tags:
        if tag.analysis_guidance:
            lines.append(f"- {tag.name}: {tag.analysis_guidance}")
    return "\n".join(lines)
