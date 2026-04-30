"""Tag management tools: create_tag, update_tag, list_tags."""

from __future__ import annotations

import re

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session
from tbc_common.db.models import Tag

log = structlog.get_logger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9_]{1,30}$")


def create_tag(
    session: Session,
    name: str,
    description: str,
    analysis_guidance: str | None = None,
) -> str:
    """Insert a new non-system Tag row.

    Validates that *name* is lowercase alphanumeric + underscore, max 30 chars,
    and that no tag with that name already exists.

    Returns a confirmation string on success.
    Raises ValueError on validation failure or duplicate name.
    """
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Tag name {name!r} is invalid — must be lowercase alphanumeric + "
            "underscore only, max 30 characters."
        )

    existing = session.get(Tag, name)
    if existing is not None:
        raise ValueError(f"Tag {name!r} already exists.")

    tag = Tag(
        name=name,
        description=description,
        analysis_guidance=analysis_guidance,
        is_active=True,
        is_system=False,
        sort_order=100,
    )
    session.add(tag)
    session.commit()
    log.info("tag.created", name=name)
    return f"Tag '{name}' created successfully."


def update_tag(
    session: Session,
    name: str,
    description: str | None = None,
    analysis_guidance: str | None = None,
    is_active: bool | None = None,
) -> str:
    """Partially update an existing Tag row.

    Fetches tag by *name*; raises ValueError if not found.
    Only supplied (non-None) fields are updated.

    Returns a confirmation string on success.
    """
    tag = session.get(Tag, name)
    if tag is None:
        raise ValueError(f"Tag {name!r} not found.")

    if description is not None:
        tag.description = description
    if analysis_guidance is not None:
        tag.analysis_guidance = analysis_guidance
    if is_active is not None:
        tag.is_active = is_active

    session.commit()
    log.info("tag.updated", name=name)
    return f"Tag '{name}' updated successfully."


def list_tags(
    session: Session,
    include_inactive: bool = False,
) -> str:
    """Return a formatted string listing all tags.

    Each entry shows name, description, analysis_guidance, and is_active.
    By default only active tags are returned; pass include_inactive=True for all.
    """
    stmt = select(Tag).order_by(Tag.sort_order.asc(), Tag.name.asc())
    if not include_inactive:
        stmt = stmt.where(Tag.is_active.is_(True))

    rows = session.execute(stmt).scalars().all()

    if not rows:
        return "No tags found."

    lines: list[str] = []
    for tag in rows:
        guidance_part = (
            f"\n    guidance: {tag.analysis_guidance}" if tag.analysis_guidance else ""
        )
        status = "active" if tag.is_active else "inactive"
        system = " [system]" if tag.is_system else ""
        lines.append(
            f"- {tag.name}{system} ({status})\n"
            f"    description: {tag.description}"
            f"{guidance_part}"
        )

    return "\n".join(lines)
