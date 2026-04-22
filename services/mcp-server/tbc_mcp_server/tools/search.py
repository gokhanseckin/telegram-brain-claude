"""search_messages and semantic_search tools."""

from __future__ import annotations

from datetime import date, datetime

import httpx
import structlog
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from tbc_common.config import settings
from tbc_common.db.models import Chat, Message, MessageUnderstanding, User

from ..models import MessageResult

log = structlog.get_logger(__name__)


def _make_deep_link(chat: Chat, message_id: int) -> str:
    """Build a tg:// deep link for a message."""
    if chat.username:
        return f"tg://resolve?domain={chat.username}&post={message_id}"
    # Private channel/group: use numeric chat_id (strip leading -100 for supergroups)
    cid = abs(chat.chat_id)
    return f"tg://privatepost?channel={cid}&msg={message_id}"


def _row_to_message_result(
    msg: Message,
    chat: Chat,
    sender: User | None,
    understanding: MessageUnderstanding | None,
) -> MessageResult:
    sender_name: str | None = None
    if sender:
        parts = [sender.first_name or "", sender.last_name or ""]
        sender_name = " ".join(p for p in parts if p).strip() or sender.username or None

    return MessageResult(
        chat_id=chat.chat_id,
        chat_title=chat.title,
        chat_tag=chat.tag,
        message_id=msg.message_id,
        sent_at=msg.sent_at,
        sender_name=sender_name,
        text=msg.text,
        summary_en=understanding.summary_en if understanding else None,
        signal_type=understanding.signal_type if understanding else None,
        url=_make_deep_link(chat, msg.message_id),
    )


def search_messages(
    db: Session,
    query: str,
    chat_ids: list[int] | None = None,
    tags: list[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sender_ids: list[int] | None = None,
    limit: int = 50,
) -> list[MessageResult]:
    """Full-text search using tsvector with optional trigram fallback."""
    # Build base query joining messages → chats → users → understanding
    stmt = (
        select(Message, Chat, User, MessageUnderstanding)
        .join(Chat, Chat.chat_id == Message.chat_id)
        .outerjoin(User, User.user_id == Message.sender_id)
        .outerjoin(
            MessageUnderstanding,
            and_(
                MessageUnderstanding.chat_id == Message.chat_id,
                MessageUnderstanding.message_id == Message.message_id,
            ),
        )
    )

    filters = [
        Message.deleted_at.is_(None),
        Chat.tag.isnot(None),
        Chat.tag != "ignore",
    ]

    if chat_ids:
        filters.append(Message.chat_id.in_(chat_ids))
    if tags:
        filters.append(Chat.tag.in_(tags))
    if date_from:
        filters.append(Message.sent_at >= datetime(date_from.year, date_from.month, date_from.day))
    if date_to:
        filters.append(
            Message.sent_at
            < datetime(date_to.year, date_to.month, date_to.day + 1)
        )
    if sender_ids:
        filters.append(Message.sender_id.in_(sender_ids))

    # tsvector search
    ts_filter = text(
        "to_tsvector('simple', COALESCE(messages.text, '')) @@ plainto_tsquery('simple', :q)"
    ).bindparams(q=query)

    tsvector_stmt = stmt.where(and_(*filters, ts_filter)).limit(limit)
    rows = db.execute(tsvector_stmt).all()

    results = [_row_to_message_result(r[0], r[1], r[2], r[3]) for r in rows]

    # Trigram fallback if no results
    if not results:
        trgm_filter = text(
            "messages.text ILIKE :pat"
        ).bindparams(pat=f"%{query}%")
        trgm_stmt = stmt.where(and_(*filters, trgm_filter)).limit(limit)
        rows = db.execute(trgm_stmt).all()
        results = [_row_to_message_result(r[0], r[1], r[2], r[3]) for r in rows]

    return results


async def semantic_search(
    db: Session,
    query: str,
    top_k: int = 20,
    chat_ids: list[int] | None = None,
    tags: list[str] | None = None,
    date_from: date | None = None,
) -> list[MessageResult]:
    """Vector similarity search using pgvector cosine distance."""
    # Get embedding from Ollama
    embed_url = settings.ollama_base_url.rstrip("/") + "/api/embed"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            embed_url,
            json={"model": settings.embedding_model, "input": query},
        )
        resp.raise_for_status()
        data = resp.json()
        # Ollama /api/embed returns {"embeddings": [[...]]}
        embeddings = data.get("embeddings") or data.get("embedding")
        if isinstance(embeddings[0], list):
            embedding = embeddings[0]
        else:
            embedding = embeddings

    # Build pgvector cosine similarity query
    stmt = (
        select(Message, Chat, User, MessageUnderstanding)
        .join(
            MessageUnderstanding,
            and_(
                MessageUnderstanding.chat_id == Message.chat_id,
                MessageUnderstanding.message_id == Message.message_id,
            ),
        )
        .join(Chat, Chat.chat_id == Message.chat_id)
        .outerjoin(User, User.user_id == Message.sender_id)
    )

    filters = [
        Message.deleted_at.is_(None),
        Chat.tag.isnot(None),
        Chat.tag != "ignore",
        MessageUnderstanding.embedding.isnot(None),
    ]

    if chat_ids:
        filters.append(Message.chat_id.in_(chat_ids))
    if tags:
        filters.append(Chat.tag.in_(tags))
    if date_from:
        filters.append(Message.sent_at >= datetime(date_from.year, date_from.month, date_from.day))

    # Order by cosine distance (closer = more similar)
    stmt = (
        stmt.where(and_(*filters))
        .order_by(MessageUnderstanding.embedding.cosine_distance(embedding))
        .limit(top_k)
    )

    rows = db.execute(stmt).all()
    return [_row_to_message_result(r[0], r[1], r[2], r[3]) for r in rows]
