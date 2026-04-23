"""Assembles cached and fresh input blocks for the Morning Brief."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session
from tbc_common.db.models import (
    BriefFeedback,
    Chat,
    Commitment,
    Message,
    MessageUnderstanding,
    RadarAlert,
    RelationshipState,
)

log = structlog.get_logger(__name__)

SIGNAL_TAXONOMY = """\
Signal taxonomy reference:
- buying_signal: prospect showing purchase intent
- expansion_signal: existing client showing upsell opportunity
- referral_signal: client likely to refer others
- cooling_signal: relationship going cold or disengaged
- competitive_threat: competitor mentioned or activity noticed
- objection: concern or blocker raised
- milestone: important event or achievement mentioned
- commitment_made: explicit promise or deadline
- commitment_received: promise received from counterparty
"""

BRIEF_FORMAT_SPEC = """\
Brief format spec:
Output exactly five sections in order:
1. 🎯 OPPORTUNITIES & RISKS — 3-7 items, each: signal sentence + action sentence + chat name
2. ⏳ YOU OWE — missed replies and open user commitments, ranked by relationship value x age
3. 📬 THEY OWE YOU — open counterparty commitments worth chasing
4. 📊 PORTFOLIO MOVEMENT — cross-chat patterns, warming/cooling relationships
5. 🧭 TODAY'S FOCUS — one paragraph, top three actions
Keep total length under 3000 characters to fit a single Telegram message.
"""


def build_cached_context(session: Session) -> str:
    """Build stable cached context: chat tags/notes + taxonomy + format spec."""
    chats = session.execute(
        select(Chat).where(Chat.tag.isnot(None)).order_by(Chat.chat_id)
    ).scalars().all()

    lines = ["## Chat Tags and Notes"]
    for chat in chats:
        title = chat.title or f"chat_{chat.chat_id}"
        line = f"- [{chat.tag}] {title}"
        if chat.notes:
            line += f" — {chat.notes}"
        lines.append(line)

    lines.append("")
    lines.append(SIGNAL_TAXONOMY)
    lines.append(BRIEF_FORMAT_SPEC)

    return "\n".join(lines)


def build_fresh_input(session: Session) -> tuple[str, list[int]]:
    """Build daily-varying fresh input. Returns (text, list of radar_alert ids included)."""
    now = datetime.now(UTC)
    yesterday = now - timedelta(hours=24)
    last_week = now - timedelta(days=7)
    today = date.today()

    lines: list[str] = []

    # --- Last 24h message understandings (non-ignored chats) ---
    lines.append("## Recent Message Intelligence (last 24h)")

    # Subquery: get chat_ids that are tagged 'ignore'
    ignored_chat_ids = [
        row[0]
        for row in session.execute(
            select(Chat.chat_id).where(Chat.tag == "ignore")
        ).all()
    ]

    mu_query = (
        select(MessageUnderstanding, Message, Chat)
        .join(Message, (Message.chat_id == MessageUnderstanding.chat_id) & (Message.message_id == MessageUnderstanding.message_id))
        .join(Chat, Chat.chat_id == MessageUnderstanding.chat_id)
        .where(Message.sent_at >= yesterday)
    )
    if ignored_chat_ids:
        mu_query = mu_query.where(MessageUnderstanding.chat_id.notin_(ignored_chat_ids))
    mu_rows = session.execute(mu_query.order_by(Message.sent_at)).all()

    for mu, msg, chat in mu_rows:
        if not mu.summary_en:
            continue
        chat_title = chat.title or f"chat_{chat.chat_id}"
        directed = " [directed at you]" if mu.is_directed_at_user else ""
        signal_info = ""
        if mu.signal_type:
            signal_info = f" | signal: {mu.signal_type} (strength={mu.signal_strength})"
        lines.append(
            f"- [{msg.sent_at.strftime('%H:%M')}] {chat_title}: {mu.summary_en}{directed}{signal_info}"
        )

    if not any(line.startswith("- ") for line in lines):
        lines.append("(no messages in last 24h)")

    # --- Open commitments ---
    lines.append("")
    lines.append("## Open Commitments")

    user_commits = session.execute(
        select(Commitment)
        .where(Commitment.status == "open")
        .where(Commitment.owner == "user")
        .order_by(Commitment.created_at)
    ).scalars().all()

    if user_commits:
        lines.append("### You owe:")
        for c in user_commits:
            due = f" (due: {c.due_at.strftime('%Y-%m-%d')})" if c.due_at else ""
            age_days = (now - c.created_at.replace(tzinfo=UTC if c.created_at.tzinfo is None else c.created_at.tzinfo)).days
            lines.append(f"- [age={age_days}d]{due} {c.description}")
    else:
        lines.append("### You owe: (none)")

    cp_commits = session.execute(
        select(Commitment)
        .where(Commitment.status == "open")
        .where(Commitment.owner == "counterparty")
        .order_by(Commitment.created_at)
    ).scalars().all()

    if cp_commits:
        lines.append("### They owe you:")
        for c in cp_commits:
            due = f" (due: {c.due_at.strftime('%Y-%m-%d')})" if c.due_at else ""
            age_days = (now - c.created_at.replace(tzinfo=UTC if c.created_at.tzinfo is None else c.created_at.tzinfo)).days
            lines.append(f"- [age={age_days}d]{due} {c.description}")
    else:
        lines.append("### They owe you: (none)")

    # --- Today's radar alerts ---
    lines.append("")
    lines.append("## Radar Alerts (last 24h)")

    alerts = session.execute(
        select(RadarAlert)
        .where(RadarAlert.created_at >= yesterday)
        .order_by(RadarAlert.severity.desc())
    ).scalars().all()

    alert_ids: list[int] = []
    if alerts:
        for alert in alerts:
            alert_ids.append(alert.id)
            tag_match = ""
            if alert.reasoning:
                import re
                m = re.search(r"#\w+", alert.reasoning)
                if m:
                    tag_match = f" {m.group(0)}"
            sev = f"[sev={alert.severity}]" if alert.severity else ""
            lines.append(f"- {sev}{tag_match} [{alert.alert_type}] {alert.title or ''}: {alert.reasoning or ''}")
    else:
        lines.append("(none)")

    # --- Relationship state deltas ---
    lines.append("")
    lines.append("## Relationship Temperature Changes (vs last week)")

    rs_changed = session.execute(
        select(RelationshipState)
        .where(RelationshipState.updated_at >= last_week)
        .order_by(RelationshipState.updated_at.desc())
    ).scalars().all()

    if rs_changed:
        for rs in rs_changed:
            chat = session.get(Chat, rs.chat_id)
            chat_title = (chat.title if chat else None) or f"chat_{rs.chat_id}"
            lines.append(
                f"- {chat_title}: stage={rs.stage}, temperature={rs.temperature} (updated {rs.updated_at.strftime('%Y-%m-%d')})"
            )
    else:
        lines.append("(no changes)")

    # --- Last 14 days brief feedback ---
    lines.append("")
    lines.append("## Brief Feedback (last 14 days) — calibration")

    two_weeks_ago = today - timedelta(days=14)
    feedbacks = session.execute(
        select(BriefFeedback)
        .where(BriefFeedback.brief_date >= two_weeks_ago)
        .order_by(BriefFeedback.brief_date.desc())
    ).scalars().all()

    if feedbacks:
        for fb in feedbacks:
            note = f" — {fb.note}" if fb.note else ""
            lines.append(f"- [{fb.brief_date}] {fb.feedback}{note} (ref: {fb.item_ref or 'n/a'})")
    else:
        lines.append("(no feedback yet)")

    lines.append("")
    lines.append(f"Today's date: {today.isoformat()}")

    return "\n".join(lines), alert_ids
