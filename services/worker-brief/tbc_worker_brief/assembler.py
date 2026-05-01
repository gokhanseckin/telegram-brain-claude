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
Signal taxonomy reference (interpret per chat tag — personal_event applies
to chats tagged family/personal, partnership applies to partner, etc.):

Business signals:
- buying: counterparty showing purchase intent (use for prospect tags)
- expansion: existing customer showing upsell opportunity
- referral: counterparty likely to refer others
- partnership: joint-venture or co-execution opening (partner tag)
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
- emotional_support: family/personal contact expressing distress or needing presence
- celebration: good news worth acknowledging
- favor_request: someone asked the user for help (non-business)
- relationship_drift: family/personal contact has gone quiet

Cross-cutting:
- commitment_made: explicit promise the user made
- commitment_received: promise the counterparty made to the user
- other: notable but doesn't fit above
"""

BRIEF_FORMAT_SPEC = """\
Brief format spec:
Output exactly five sections in order:
1. 🌅 THE SHAPE OF TODAY — one short paragraph; honest tone of the day
2. ✅ ON YOUR PLATE — others waiting on user; mix work + personal; rank by waiting-time x importance. CRITICAL: when an Open Commitments input row carries a `(c<id>)` tag, INCLUDE that tag inline at the end of the bullet so the user can reply later. Format: `• <description>. <context>. (c<id>)`. Drop the tag only for items synthesized from raw 24h messages that have no commitment row.
3. 🔔 WAITING ON OTHERS — user waiting on others; flag chase-worthy and say HOW to nudge. Same `(c<id>)` rule as #2: preserve the tag from any Open Commitments row.
4. 💡 WORTH NOTICING — 3-6 cross-chat signals (business AND personal); name signal + human response + chat. CRITICAL: when the underlying input row carries a `ref=#xxxx` tag (radar alerts), include that tag in parentheses IMMEDIATELY after the specific observation it refers to — not at the end of the whole bullet. Each radar alert must appear as its own observation with its own (#xxxx) inline. NEVER merge multiple (#xxxx) tags at the end of a bullet. Format: `• [Name / @username / tag] — <observation A> (#xxxx). <observation B> (#yyyy).`. Items synthesized from raw 24h messages without a ref tag get no parenthetical. Avoid redundancy: skip a person from WORTH NOTICING if they already appear in ON YOUR PLATE or WAITING ON OTHERS and the signal adds nothing beyond the commitment already surfaced.
5. 🎯 IF YOU ONLY DO THREE THINGS — one paragraph, the three moves
Note: relationship temperature/state changes are still provided in the
input as background context — fold relevant ones into "WORTH NOTICING"
(if signal-shaped) or "ON YOUR PLATE" (if a nudge is owed). Do NOT
write a dedicated temperature section.
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


def render_commitment(c: Commitment, *, now: datetime, chat_label: str = "") -> str:
    """Render an Open Commitments line for the brief input.

    `chat_label` is the human-readable counterparty identifier
    (e.g., "Barış / @baris / personal") so the brief LLM can name the
    person who owes / is owed. Without it the model just sees the
    description and surfaces "Someone".

    The trailing `(c<id>)` short-id is the load-bearing UX hook: the
    LLM is instructed to preserve it inline in ON YOUR PLATE / WAITING
    ON OTHERS, and the user can later mark a commitment with that
    handle (today: by talking to Claude / agent; future: a `/done c42`
    rules-path slash, see queued PR3 work).

    `c` prefix disambiguates from radar's hex `#xxxx` tags.
    """
    # Prefer source_sent_at (when the conversation actually happened)
    # over created_at (when the extractor wrote the row). Fall back to
    # created_at only if the column hasn't been backfilled yet.
    anchor = c.source_sent_at or c.created_at
    anchor = anchor if anchor.tzinfo else anchor.replace(tzinfo=UTC)
    age_days = (now - anchor).days
    date_label = anchor.strftime("%Y-%m-%d")
    date_kind = "from" if c.source_sent_at else "extracted"
    due = f" (due: {c.due_at.strftime('%Y-%m-%d')})" if c.due_at else " (no due date)"
    chat_part = f" [with {chat_label}]" if chat_label else ""
    return f"- [{date_kind} {date_label}, age={age_days}d]{due}{chat_part} (c{c.id}) {c.description}"


def build_cached_context(session: Session) -> str:
    """Build stable cached context: chat tags/notes + taxonomy + format spec."""
    chats = session.execute(
        select(Chat).where(Chat.tag.isnot(None)).order_by(Chat.chat_id)
    ).scalars().all()

    lines = ["## Chat Tags and Notes"]
    for chat in chats:
        title = chat.title or f"chat_{chat.chat_id}"
        username_part = f" / @{chat.username}" if chat.username else ""
        line = f"- [{chat.tag}] {title}{username_part}"
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

    # Pre-fetch chat labels so render_commitment can include the counterparty
    # name (otherwise the brief LLM can only say "Someone").
    chat_label_cache: dict[int, str] = {}

    def _label_for(chat_id: int | None) -> str:
        if chat_id is None:
            return ""
        if chat_id in chat_label_cache:
            return chat_label_cache[chat_id]
        ch = session.get(Chat, chat_id)
        if ch is None:
            label = f"chat_{chat_id}"
        else:
            title = ch.title or f"chat_{chat_id}"
            uname = f" / @{ch.username}" if ch.username else ""
            tag = f" / {ch.tag}" if ch.tag else ""
            label = f"{title}{uname}{tag}"
        chat_label_cache[chat_id] = label
        return label

    def _render_commit(c: Commitment) -> str:
        return render_commitment(c, now=now, chat_label=_label_for(c.chat_id))

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

    # Filter on the underlying conversation's freshness, not the alert row's
    # created_at — the radar worker may have just minted an alert from a
    # months-old understanding. Fall back to created_at only for legacy rows
    # whose source_sent_at hasn't been backfilled.
    alert_recent_filter = sa_or(
        RadarAlert.source_sent_at >= yesterday,
        sa_and(RadarAlert.source_sent_at.is_(None), RadarAlert.created_at >= yesterday),
    )
    alerts = session.execute(
        select(RadarAlert)
        .where(alert_recent_filter)
        .order_by(RadarAlert.severity.desc())
    ).scalars().all()

    alert_ids: list[int] = []
    if alerts:
        for alert in alerts:
            alert_ids.append(alert.id)
            # Surface the alert's #xxxx ref tag prominently — the brief writer
            # MUST carry it through to the output so the user can later DM
            # /feedback #xxxx to rate the item.
            ref_tag = ""
            if alert.reasoning:
                m = re.search(r"#\w+", alert.reasoning)
                if m:
                    ref_tag = m.group(0)
            sev = f"[sev={alert.severity}]" if alert.severity else ""
            anchor = alert.source_sent_at or alert.created_at
            anchor_label = anchor.strftime("%Y-%m-%d %H:%M") if anchor else "?"
            anchor_kind = "source" if alert.source_sent_at else "extracted"
            lines.append(
                f"- ref={ref_tag} [{anchor_kind} {anchor_label}] {sev} "
                f"[{alert.alert_type}] {alert.title or ''}: {alert.reasoning or ''}"
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
