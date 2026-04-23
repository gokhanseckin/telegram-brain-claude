"""Feedback handler — /feedback command.

Parses two forms:
  /feedback #a7f2 not_useful "just smalltalk"
  /feedback missed "acme mentioned budget twice"
"""

from __future__ import annotations

import re
from datetime import date

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from tbc_common.db.models import BriefFeedback
from tbc_common.db.session import get_sessionmaker

from tbc_bot.guards import is_owner

log = structlog.get_logger(__name__)

router = Router(name="feedback")

# Pattern 1: /feedback #<ref> <feedback_type> ["optional note"]
_REF_PATTERN = re.compile(
    r"^#?(?P<ref>[0-9a-fA-F]{4,8})\s+"
    r"(?P<ftype>\S+)"
    r'(?:\s+"?(?P<note>[^"]+)"?)?$'
)

# Pattern 2: /feedback missed ["note"]
_MISSED_PATTERN = re.compile(
    r'^missed\s+"?(?P<note>[^"]+)"?$',
    re.IGNORECASE,
)


@router.message(Command("feedback"))
async def cmd_feedback(message: Message) -> None:
    if not is_owner(message):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Usage:\n"
            "  /feedback #a7f2 not_useful \"just smalltalk\"\n"
            "  /feedback missed \"acme mentioned budget twice\""
        )
        return

    body = parts[1].strip()

    item_ref: str | None = None
    feedback_type: str
    note: str | None = None

    # Try missed pattern first
    missed_m = _MISSED_PATTERN.match(body)
    if missed_m:
        feedback_type = "missed_important"
        note = missed_m.group("note").strip() if missed_m.group("note") else None
    else:
        ref_m = _REF_PATTERN.match(body)
        if ref_m:
            item_ref = ref_m.group("ref").lower()
            ftype_raw = ref_m.group("ftype").lower().replace("-", "_")
            # Normalise common aliases
            if ftype_raw in ("not_useful", "notuseful", "no"):
                feedback_type = "not_useful"
            elif ftype_raw in ("useful", "yes", "good"):
                feedback_type = "useful"
            elif ftype_raw in ("missed", "missed_important"):
                feedback_type = "missed_important"
            else:
                feedback_type = ftype_raw
            note_raw = ref_m.group("note")
            note = note_raw.strip().strip('"') if note_raw else None
        else:
            await message.answer(
                "Could not parse feedback. Examples:\n"
                "  /feedback #a7f2 not_useful \"just smalltalk\"\n"
                "  /feedback missed \"acme mentioned budget twice\""
            )
            return

    today = date.today()
    row = BriefFeedback(
        brief_date=today,
        item_ref=item_ref,
        feedback=feedback_type,
        note=note,
    )

    sm = get_sessionmaker()
    with sm() as session:
        session.add(row)
        session.commit()
        session.refresh(row)

    log.info("feedback_stored", id=row.id, ref=item_ref, feedback=feedback_type)
    await message.answer(f"Feedback recorded (id={row.id}).")
