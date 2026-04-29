"""Assembles cached and fresh input blocks for the Morning Brief."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy import and_ as sa_and
from sqlalchemy import or_ as sa_or
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
Signal taxonomy reference (interpret per chat tag — supplier_issue applies
to chats tagged 'supplier', personal_event applies to friend/family/personal):

Business signals:
- buying: counterparty showing purchase intent (use for prospect/client tags)
- expansion: existing client showing upsell opportunity
- referral: counterparty likely to refer others
- partnership: joint-venture or co-execution opening (partner tag)
- supplier_issue: quality/timing/price problem from a supplier
- procurement: user needs to buy something
- competitor: competitor mentioned or active
- objection: concern or blocker raised
- pricing: pricing or budget tension
- timeline: deadline or schedule shift
- decision_maker: new stakeholder revealed
- cooling: business relationship going quiet
- risk: generic threat to a deal or working relationship
- milestone: meaningful event or achievement

Personal signals:
- personal_event: birthday, illness, big life change
- emotional_support: friend/family expressing distress or needing presence
- celebration: good news worth acknowledging
- favor_request: someone asked the user for help (non-business)
- relationship_drift: friend/family contact has gone quiet

Cross-cutting:
- commitment_made: explicit promise the user made
- commitment_received: promise the counterparty made to the user
- other: notable but doesn't fit above
"""

BRIEF_FORMAT_SPEC = """\
Brief format spec:
Output exactly six sections in order:
1. 🌅 THE SHAPE OF TODAY — one short paragraph; honest tone of the day
2. ✅ ON YOUR PLATE — others waiting on user; mix work + personal; rank by waiting-time x importance
3. 🔔 WAITING ON OTHERS — user waiting on others; flag chase-worthy and say HOW to nudge
4. 💡 WORTH NOTICING — 3-6 cross-chat signals (business AND personal); name signal + human response + chat
5. 🌡️ TEMPERATURE CHECK — warming/cooling relationships across both ledgers
6. 🎯 IF YOU ONLY DO THREE THINGS — one paragraph, the three moves
Recency rules (strict):
- The fresh input begins with "Today is YYYY-MM-DD". Anchor every claim
  against that date. Never imply something is "now" or "today" if its
  timestamp is from a previous day.
- When you reference an item, mention its age explicitly if it is more
  than 24 hours old: "from 3 days ago", "set 2 weeks back".
- Items >7 days old must be qualified ("from last week", "still open
  from earlier this month") — never written as if they just happened.
- Items >30 days old are background context only, not action items,
  unless the data shows something just changed (a new message in the
  last 24h).
- Items >90 days old are pre-filtered out of the input. If you somehow
  see one, treat it as historical reference, never as today's work.
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

    # --- Anchor: today's date is the FIRST thing the LLM sees, so every
    # `age=Nd` and `created_at` below is interpretable against it. ---
    lines.append(f"## Today is {today.isoformat()} ({now.strftime('%A')}). Now: {now.strftime('%Y-%m-%d %H:%M UTC')}.")
    lines.append(
        "All timestamps below are real wall-clock times. Frame urgency "
        "against `Now` — a timestamp from yesterday is yesterday, not today."
    )
    lines.append("")

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
        tag_prefix = f"[{chat.tag}] " if chat.tag else ""
        directed = " [directed at you]" if mu.is_directed_at_user else ""
        signal_info = ""
        if mu.signal_type:
            signal_info = f" | signal: {mu.signal_type} (strength={mu.signal_strength})"
        # Full timestamp so the LLM never has to guess which day a message
        # is from. Add a hours-ago hint for fast scanning.
        sent = msg.sent_at if msg.sent_at.tzinfo else msg.sent_at.replace(tzinfo=UTC)
        hours_ago = int((now - sent).total_seconds() // 3600)
        ago_hint = f" ({hours_ago}h ago)" if hours_ago > 0 else " (just now)"
        lines.append(
            f"- [{sent.strftime('%Y-%m-%d %H:%M')}{ago_hint}] {tag_prefix}{chat_title}: "
            f"{mu.summary_en}{directed}{signal_info}"
        )

    if not any(line.startswith("- ") for line in lines):
        lines.append("(no messages in last 24h)")

    # --- Open commitments ---
    lines.append("")
    lines.append("## Open Commitments")

    ninety_days_ago = now - timedelta(days=90)

    def _render_commit(c: Commitment) -> str:
        # Prefer source_sent_at (when the conversation actually happened) over
        # created_at (when the extractor wrote the row). Fall back to created_at
        # only if the column hasn't been backfilled yet.
        anchor = c.source_sent_at or c.created_at
        anchor = anchor if anchor.tzinfo else anchor.replace(tzinfo=UTC)
        age_days = (now - anchor).days
        date_label = anchor.strftime("%Y-%m-%d")
        date_kind = "from" if c.source_sent_at else "extracted"
        due = f" (due: {c.due_at.strftime('%Y-%m-%d')})" if c.due_at else " (no due date)"
        return f"- [{date_kind} {date_label}, age={age_days}d]{due} {c.description}"

    # Recency filter on the true conversation date when available, falling back
    # to created_at when source_sent_at is unset (e.g. row predates backfill).
    recent_filter = sa_or(
        Commitment.source_sent_at >= ninety_days_ago,
        sa_and(Commitment.source_sent_at.is_(None), Commitment.created_at >= ninety_days_ago),
    )

    user_commits = session.execute(
        select(Commitment)
        .where(Commitment.status == "open")
        .where(Commitment.owner == "user")
        .where(recent_filter)
        .order_by(Commitment.source_sent_at.nullslast(), Commitment.created_at)
    ).scalars().all()

    if user_commits:
        lines.append("### You owe:")
        for c in user_commits:
            lines.append(_render_commit(c))
    else:
        lines.append("### You owe: (none)")

    cp_commits = session.execute(
        select(Commitment)
        .where(Commitment.status == "open")
        .where(Commitment.owner == "counterparty")
        .where(recent_filter)
        .order_by(Commitment.source_sent_at.nullslast(), Commitment.created_at)
    ).scalars().all()

    if cp_commits:
        lines.append("### They owe you:")
        for c in cp_commits:
            lines.append(_render_commit(c))
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
                m = re.search(r"#\w+", alert.reasoning)
                if m:
                    tag_match = f" {m.group(0)}"
            sev = f"[sev={alert.severity}]" if alert.severity else ""
            created = alert.created_at.strftime("%Y-%m-%d %H:%M") if alert.created_at else "?"
            lines.append(
                f"- [{created}] {sev}{tag_match} [{alert.alert_type}] "
                f"{alert.title or ''}: {alert.reasoning or ''}"
            )
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

    return "\n".join(lines), alert_ids
