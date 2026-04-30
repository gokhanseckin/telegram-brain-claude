"""Executors — turn a RouterDecision into a side effect + user-facing reply.

Stage 2 PR1 ships only the feedback executor. PR2 will add commitment
executors with echo-back state for destructive intents.

Executors run synchronously inside `asyncio.to_thread` because the
bot is async but SQLAlchemy sessions are sync. The DB write itself is
short (single INSERT), so this is fine — no need for an async driver.
"""

from __future__ import annotations

import asyncio
from datetime import date as date_cls

import structlog
from tbc_common.db.models import BriefFeedback
from tbc_common.db.session import get_sessionmaker

from .decision import RouterDecision

log = structlog.get_logger(__name__)


def _write_feedback_sync(
    feedback_type: str,
    item_ref: str | None,
    note: str | None,
) -> int:
    """Insert a brief_feedback row. Returns the new row id.

    Mirrors the column population in
    `tbc_mcp_server.tools.feedback.write_brief_feedback` and the legacy
    `/feedback` slash handler — same table, same shape, so brief
    calibration treats all three paths uniformly.
    """
    sm = get_sessionmaker()
    with sm() as session:
        row = BriefFeedback(
            brief_date=date_cls.today(),
            item_ref=item_ref,
            feedback=feedback_type,
            note=note,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return int(row.id)


async def exec_feedback(decision: RouterDecision) -> str:
    """Persist a feedback row and return the user-facing confirmation text.

    Expects `decision.fields` to contain `feedback_type`, `item_ref`,
    `note`. Validation of `feedback_type` happened upstream (in the rules
    vocab set or, in PR2, the LLM schema check) so we trust it here.
    """
    feedback_type = decision.fields["feedback_type"]
    item_ref = decision.fields.get("item_ref")
    note = decision.fields.get("note")

    row_id = await asyncio.to_thread(
        _write_feedback_sync, feedback_type, item_ref, note
    )

    log.info(
        "router_feedback_written",
        id=row_id,
        item_ref=item_ref,
        feedback=feedback_type,
        source=decision.source,
    )

    if item_ref:
        return f"Recorded: {feedback_type} on #{item_ref} (id={row_id})."
    return f"Recorded missed: {note or '(no note)'} (id={row_id})."
