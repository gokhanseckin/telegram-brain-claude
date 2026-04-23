"""Assembles weekly input for the Weekly Review batch job."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session
from tbc_common.db.models import (
    BriefFeedback,
    Chat,
    ChatSummary,
    Commitment,
    RadarAlert,
    RelationshipState,
)

log = structlog.get_logger(__name__)


def monday_of_week(d: date) -> date:
    """Return the Monday of the ISO week containing d."""
    return d - timedelta(days=d.weekday())


def build_weekly_input(session: Session) -> str:
    """Assemble the weekly review input text."""
    today = date.today()
    now = datetime.now(UTC)
    week_ago = now - timedelta(days=7)
    monday = monday_of_week(today)

    lines: list[str] = []
    lines.append(f"Weekly Review for week starting {monday.isoformat()} (generated {today.isoformat()})")
    lines.append("")

    # --- Last 7 days of chat summaries (period='day') ---
    lines.append("## Daily Chat Summaries (last 7 days)")

    summaries = session.execute(
        select(ChatSummary, Chat)
        .join(Chat, Chat.chat_id == ChatSummary.chat_id, isouter=True)
        .where(ChatSummary.period == "day")
        .where(ChatSummary.period_start >= monday - timedelta(days=7))
        .order_by(ChatSummary.period_start, ChatSummary.chat_id)
    ).all()

    if summaries:
        for summary, chat in summaries:
            chat_title = (chat.title if chat else None) or f"chat_{summary.chat_id}"
            lines.append(f"### [{summary.period_start}] {chat_title}")
            lines.append(summary.summary)
            lines.append("")
    else:
        lines.append("(no daily summaries available)")
        lines.append("")

    # --- All commitments from the past week ---
    lines.append("## Commitments (past week)")

    commits = session.execute(
        select(Commitment)
        .where(Commitment.created_at >= week_ago)
        .order_by(Commitment.owner, Commitment.created_at)
    ).scalars().all()

    if commits:
        for c in commits:
            status_str = c.status
            if c.resolved_at:
                status_str = f"resolved {c.resolved_at.strftime('%Y-%m-%d')}"
            due = f" (due: {c.due_at.strftime('%Y-%m-%d')})" if c.due_at else ""
            lines.append(f"- [{c.owner}] [{status_str}]{due} {c.description}")
    else:
        lines.append("(none)")
    lines.append("")

    # --- All radar alerts from the past week ---
    lines.append("## Radar Alerts (past week)")

    alerts = session.execute(
        select(RadarAlert)
        .where(RadarAlert.created_at >= week_ago)
        .order_by(RadarAlert.severity.desc(), RadarAlert.created_at)
    ).scalars().all()

    if alerts:
        for alert in alerts:
            sev = f"[sev={alert.severity}]" if alert.severity else ""
            surfaced = " [surfaced in brief]" if alert.surfaced_in_brief_at else ""
            lines.append(f"- {sev} [{alert.alert_type}]{surfaced} {alert.title or ''}: {alert.reasoning or ''}")
    else:
        lines.append("(none)")
    lines.append("")

    # --- Relationship state deltas ---
    lines.append("## Relationship State Changes (past week)")

    rs_changed = session.execute(
        select(RelationshipState)
        .where(RelationshipState.updated_at >= week_ago)
        .order_by(RelationshipState.updated_at.desc())
    ).scalars().all()

    if rs_changed:
        for rs in rs_changed:
            chat = session.get(Chat, rs.chat_id)
            chat_title = (chat.title if chat else None) or f"chat_{rs.chat_id}"
            lines.append(
                f"- {chat_title}: stage={rs.stage}, temperature={rs.temperature} "
                f"(updated {rs.updated_at.strftime('%Y-%m-%d')})"
            )
    else:
        lines.append("(no changes)")
    lines.append("")

    # --- Brief feedback from the past week ---
    lines.append("## Brief Feedback (past week)")

    feedbacks = session.execute(
        select(BriefFeedback)
        .where(BriefFeedback.brief_date >= today - timedelta(days=7))
        .order_by(BriefFeedback.brief_date.desc())
    ).scalars().all()

    if feedbacks:
        for fb in feedbacks:
            note = f" — {fb.note}" if fb.note else ""
            lines.append(f"- [{fb.brief_date}] {fb.feedback}{note} (ref: {fb.item_ref or 'n/a'})")
    else:
        lines.append("(no feedback)")

    return "\n".join(lines)
