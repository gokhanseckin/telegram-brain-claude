"""FastAPI application with MCP server mounted at /mcp.

Streamable HTTP transport via the mcp Python SDK.
Auth: Bearer token middleware (see auth.py).
"""

from __future__ import annotations

import contextlib
import json
from datetime import date, datetime
from typing import cast

import structlog
import uvicorn
from fastapi import FastAPI
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.routing import Mount
from tbc_common.logging import configure_logging

from .auth import BearerTokenMiddleware
from .tools.brief import get_recent_brief
from .tools.chat import get_chat_history, get_chat_summary, list_chats
from .tools.commitments import (
    CommitmentNotFound,
    cancel_commitment,
    get_commitments,
    resolve_commitment,
    update_commitment,
)
from .tools.feedback import InvalidFeedbackType, write_brief_feedback
from .tools.relationship import get_relationship_state
from .tools.search import search_messages, semantic_search
from .tools.signals import get_signals

configure_logging("mcp-server")
log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# MCP Server definition
# ---------------------------------------------------------------------------

mcp_server = Server("tbc-mcp-server")


def _db() -> Session:
    """Get a DB session (used inside MCP tool handlers)."""
    from tbc_common.db.session import get_sessionmaker
    return cast(Session, get_sessionmaker()())


@mcp_server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
async def handle_list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_messages",
            description=(
                "Full-text search over Telegram messages. "
                "Uses PostgreSQL tsvector with trigram fallback."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "chat_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Filter by chat IDs",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by chat tags (client, prospect, supplier, partner, internal, friend, family, personal)",
                    },
                    "date_from": {"type": "string", "format": "date", "description": "Start date (YYYY-MM-DD)"},
                    "date_to": {"type": "string", "format": "date", "description": "End date (YYYY-MM-DD)"},
                    "sender_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Filter by sender user IDs",
                    },
                    "limit": {"type": "integer", "default": 50, "description": "Max results"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="semantic_search",
            description=(
                "Semantic/vector search over messages using pgvector cosine similarity. "
                "Generates embeddings via Ollama bge-m3."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language query"},
                    "top_k": {"type": "integer", "default": 20, "description": "Max results"},
                    "chat_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "date_from": {"type": "string", "format": "date"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_chat_history",
            description="Paginated message history for a single chat, newest first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "integer"},
                    "before": {
                        "type": "string",
                        "format": "date-time",
                        "description": "Return messages before this timestamp (pagination cursor)",
                    },
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["chat_id"],
            },
        ),
        Tool(
            name="list_chats",
            description="List all tracked chats with tag, last activity, participant count.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "Filter by tag (client, prospect, supplier, partner, internal, friend, family, personal)",
                    },
                    "include_untagged": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include chats without a tag",
                    },
                },
            },
        ),
        Tool(
            name="get_chat_summary",
            description="Pre-computed daily or weekly summaries for a chat.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "integer"},
                    "period": {
                        "type": "string",
                        "enum": ["day", "week"],
                        "default": "week",
                    },
                    "periods_back": {
                        "type": "integer",
                        "default": 1,
                        "description": "How many periods back to fetch",
                    },
                },
                "required": ["chat_id"],
            },
        ),
        Tool(
            name="get_commitments",
            description=(
                "Query tracked commitments (promises made or received). "
                "Use `ids` for direct lookup when the user references one or "
                "more commitments by their `c<id>` short tag (the brief and "
                "/done /cancel shortcuts surface these). Use `query` for "
                "natural-language matching when the user mentions a topic "
                "instead ('the report', '67.05', 'Bob'). The user has "
                "hundreds of open commitments, never load them all."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "Direct lookup by commitment id. Pass the integer "
                            "after the `c` prefix (e.g. `c9273` -> 9273)."
                        ),
                    },
                    "status": {
                        "type": "string",
                        "enum": ["open", "done", "cancelled", "stale"],
                    },
                    "owner": {
                        "type": "string",
                        "enum": ["user", "counterparty"],
                    },
                    "chat_id": {"type": "integer"},
                    "overdue_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Only return overdue open commitments",
                    },
                    "query": {
                        "type": "string",
                        "description": "Case-insensitive substring match on description",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max rows returned (default 50)",
                    },
                },
            },
        ),
        Tool(
            name="resolve_commitment",
            description=(
                "Mark a commitment as DONE. Use when the user says they've "
                "completed something (e.g. 'I sent the report', 'paid Gizem'). "
                "ALWAYS find the commitment via get_commitments(query=...) first "
                "and confirm the right id; if there's ambiguity, list the "
                "candidates back to the user before calling this. Sets "
                "status='done' and records the note + the user's message id "
                "for audit."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "commitment_id": {
                        "type": "integer",
                        "description": "Exact commitment.id to close",
                    },
                    "note": {
                        "type": "string",
                        "description": "User's resolution wording, e.g. 'sent the report today'",
                    },
                    "resolved_by_message_id": {
                        "type": "integer",
                        "description": "Telegram message id that triggered the resolution (optional)",
                    },
                },
                "required": ["commitment_id"],
            },
        ),
        Tool(
            name="cancel_commitment",
            description=(
                "Mark a commitment as CANCELLED — no longer relevant, not done. "
                "Use when the user explicitly says to drop it ('forget that', "
                "'no longer needed', 'overcome by events'). Find via "
                "get_commitments first; never guess."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "commitment_id": {"type": "integer"},
                    "reason": {
                        "type": "string",
                        "description": "Why it was cancelled, for audit",
                    },
                },
                "required": ["commitment_id"],
            },
        ),
        Tool(
            name="update_commitment",
            description=(
                "Adjust an open commitment without closing it. Use to set or "
                "push a due date ('move to next Friday') or to append a "
                "status note ('waiting on Bob's reply'). Either due_at or "
                "note_append must be provided."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "commitment_id": {"type": "integer"},
                    "due_at": {
                        "type": "string",
                        "format": "date-time",
                        "description": "ISO 8601 UTC timestamp for the new due date",
                    },
                    "note_append": {
                        "type": "string",
                        "description": "Free-text note to append to the description",
                    },
                },
                "required": ["commitment_id"],
            },
        ),
        Tool(
            name="write_brief_feedback",
            description=(
                "Record user feedback on a Morning Brief item. Use when the "
                "user reacts to brief content in plain language ('the #ab12 "
                "was useful', 'not useful, just smalltalk', 'you missed the "
                "Acme thing'). Mirrors /feedback slash command — both paths "
                "write the same brief_feedback row, which calibrates the next "
                "brief. If the user references a `#xxxx` tag, pass it as "
                "item_ref. If they're reporting something missing without a "
                "tag, set feedback_type='missed_important' and put their "
                "phrasing in note. Always confirm in your reply what was "
                "written."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "feedback_type": {
                        "type": "string",
                        "enum": ["useful", "not_useful", "missed_important"],
                        "description": (
                            "useful = item was worth surfacing; "
                            "not_useful = noise/already-known; "
                            "missed_important = brief should have surfaced this."
                        ),
                    },
                    "item_ref": {
                        "type": "string",
                        "description": (
                            "The `#xxxx` tag from the brief (with or without "
                            "the leading #). Required for useful/not_useful; "
                            "optional for missed_important."
                        ),
                    },
                    "note": {
                        "type": "string",
                        "description": (
                            "User's free-text reasoning, e.g. 'just smalltalk' "
                            "or 'this should have been bigger'."
                        ),
                    },
                },
                "required": ["feedback_type"],
            },
        ),
        Tool(
            name="get_signals",
            description="Query signals detected in messages — business and personal.",
            inputSchema={
                "type": "object",
                "properties": {
                    "signal_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Business: buying, expansion, referral, partnership, supplier_issue, procurement, competitor, objection, pricing, timeline, decision_maker, cooling, risk, milestone. Personal: personal_event, emotional_support, celebration, favor_request, relationship_drift. Cross-cutting: commitment_made, commitment_received, other.",
                    },
                    "min_strength": {
                        "type": "integer",
                        "default": 1,
                        "minimum": 1,
                        "maximum": 5,
                    },
                    "date_from": {"type": "string", "format": "date"},
                    "chat_ids": {"type": "array", "items": {"type": "integer"}},
                },
            },
        ),
        Tool(
            name="get_relationship_state",
            description="Inferred relationship state per chat: stage, temperature, open threads.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "integer",
                        "description": "Omit to return all chats",
                    },
                },
            },
        ),
        Tool(
            name="get_recent_brief",
            description="Return the most recent (or specific-date) Morning Brief content.",
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "format": "date",
                        "description": "Date of the brief (YYYY-MM-DD). Omit for latest.",
                    },
                },
            },
        ),
    ]


@mcp_server.call_tool()  # type: ignore[untyped-decorator]
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:  # type: ignore[type-arg]
    db = _db()
    try:
        result = await _dispatch_tool(name, arguments, db)
    finally:
        db.close()

    return [TextContent(type="text", text=json.dumps(result, default=str))]


async def _dispatch_tool(name: str, args: dict, db: Session) -> object:  # type: ignore[type-arg]
    if name == "search_messages":
        date_from = date.fromisoformat(args["date_from"]) if args.get("date_from") else None
        date_to = date.fromisoformat(args["date_to"]) if args.get("date_to") else None
        results = search_messages(
            db,
            query=args["query"],
            chat_ids=args.get("chat_ids"),
            tags=args.get("tags"),
            date_from=date_from,
            date_to=date_to,
            sender_ids=args.get("sender_ids"),
            limit=args.get("limit", 50),
        )
        return [r.model_dump() for r in results]

    elif name == "semantic_search":
        date_from = date.fromisoformat(args["date_from"]) if args.get("date_from") else None
        results = await semantic_search(
            db,
            query=args["query"],
            top_k=args.get("top_k", 20),
            chat_ids=args.get("chat_ids"),
            tags=args.get("tags"),
            date_from=date_from,
        )
        return [r.model_dump() for r in results]

    elif name == "get_chat_history":
        before = None
        if args.get("before"):
            before = datetime.fromisoformat(args["before"])
        results = get_chat_history(
            db,
            chat_id=args["chat_id"],
            before=before,
            limit=args.get("limit", 50),
        )
        return [r.model_dump() for r in results]

    elif name == "list_chats":
        chat_items = list_chats(
            db,
            tag=args.get("tag"),
            include_untagged=args.get("include_untagged", False),
        )
        return [r.model_dump() for r in chat_items]

    elif name == "get_chat_summary":
        summaries = get_chat_summary(
            db,
            chat_id=args["chat_id"],
            period=args.get("period", "week"),
            periods_back=args.get("periods_back", 1),
        )
        return [r.model_dump() for r in summaries]

    elif name == "get_commitments":
        commitments = get_commitments(
            db,
            ids=args.get("ids"),
            status=args.get("status"),
            owner=args.get("owner"),
            chat_id=args.get("chat_id"),
            overdue_only=args.get("overdue_only", False),
            query=args.get("query"),
            limit=args.get("limit", 50),
        )
        return [r.model_dump() for r in commitments]

    elif name == "resolve_commitment":
        try:
            result = resolve_commitment(
                db,
                commitment_id=args["commitment_id"],
                note=args.get("note"),
                resolved_by_message_id=args.get("resolved_by_message_id"),
            )
        except CommitmentNotFound as exc:
            return {"error": str(exc)}
        return result.model_dump()

    elif name == "cancel_commitment":
        try:
            result = cancel_commitment(
                db,
                commitment_id=args["commitment_id"],
                reason=args.get("reason"),
            )
        except CommitmentNotFound as exc:
            return {"error": str(exc)}
        return result.model_dump()

    elif name == "update_commitment":
        due_at = (
            datetime.fromisoformat(args["due_at"]) if args.get("due_at") else None
        )
        try:
            result = update_commitment(
                db,
                commitment_id=args["commitment_id"],
                due_at=due_at,
                note_append=args.get("note_append"),
            )
        except (CommitmentNotFound, ValueError) as exc:
            return {"error": str(exc)}
        return result.model_dump()

    elif name == "write_brief_feedback":
        try:
            fb_result = write_brief_feedback(
                db,
                feedback_type=args["feedback_type"],
                item_ref=args.get("item_ref"),
                note=args.get("note"),
            )
        except InvalidFeedbackType as exc:
            return {"error": str(exc)}
        return fb_result.model_dump()

    elif name == "get_signals":
        date_from = date.fromisoformat(args["date_from"]) if args.get("date_from") else None
        signals = get_signals(
            db,
            signal_types=args.get("signal_types"),
            min_strength=args.get("min_strength", 1),
            date_from=date_from,
            chat_ids=args.get("chat_ids"),
        )
        return [r.model_dump() for r in signals]

    elif name == "get_relationship_state":
        rel_state = get_relationship_state(
            db,
            chat_id=args.get("chat_id"),
        )
        return [r.model_dump() for r in rel_state]

    elif name == "get_recent_brief":
        date_filter = date.fromisoformat(args["date"]) if args.get("date") else None
        brief = get_recent_brief(db, date_filter=date_filter)
        return brief.model_dump()

    else:
        raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# FastAPI app (health + auth middleware)
# ---------------------------------------------------------------------------

fastapi_app = FastAPI(title="TBC MCP Server", version="0.1.0")


@fastapi_app.get("/health")
async def health() -> dict:  # type: ignore[type-arg]
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Starlette app with MCP mounted at /mcp
# ---------------------------------------------------------------------------

session_manager = StreamableHTTPSessionManager(
    app=mcp_server,
    event_store=None,
    json_response=False,
    stateless=True,
)


class _Router:
    """Dispatch /mcp and /mcp/* to the MCP session manager; everything else to FastAPI.

    Uses Mount("") so the full original path reaches the session manager without
    stripping — Starlette's Mount("/mcp") only matches /mcp/ (trailing slash required).
    """

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        path: str = scope.get("path", "")
        if scope["type"] == "http" and (path == "/mcp" or path.startswith("/mcp/")):
            await session_manager.handle_request(scope, receive, send)
        else:
            await fastapi_app(scope, receive, send)


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):  # type: ignore[no-untyped-def]
    from tbc_common.config import settings as _settings

    if not _settings.mcp_bearer_token or not _settings.mcp_bearer_token.get_secret_value():
        raise RuntimeError("TBC_MCP_BEARER_TOKEN must be set to a non-empty value")

    log.info("Starting TBC MCP Server")
    async with session_manager.run():
        yield
    log.info("TBC MCP Server stopped")


app = Starlette(
    lifespan=lifespan,
    routes=[Mount("", app=_Router())],
)

# Bearer token middleware protects both /mcp and FastAPI routes
app.add_middleware(BearerTokenMiddleware)


if __name__ == "__main__":
    uvicorn.run(
        "tbc_mcp_server.main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
