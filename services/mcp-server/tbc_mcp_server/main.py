"""FastAPI application with MCP server mounted at /mcp.

Streamable HTTP transport via the mcp Python SDK.
Auth: Bearer token middleware (see auth.py).
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime

import structlog
import uvicorn
from fastapi import FastAPI
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from tbc_common.logging import configure_logging

from .auth import BearerTokenMiddleware
from .tools.brief import get_recent_brief
from .tools.chat import get_chat_history, get_chat_summary, list_chats
from .tools.commitments import get_commitments
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
    return get_sessionmaker()()


@mcp_server.list_tools()
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
                        "description": "Filter by chat tags (client, prospect, colleague, personal)",
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
                        "description": "Filter by tag (client, prospect, colleague, personal)",
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
            description="Query tracked commitments (promises made or received).",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["open", "fulfilled", "stale", "dismissed"],
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
                },
            },
        ),
        Tool(
            name="get_signals",
            description="Query business signals detected in messages (buying intent, risk, etc.).",
            inputSchema={
                "type": "object",
                "properties": {
                    "signal_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "e.g. buying, risk, expansion, competitor, referral, cooling",
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


@mcp_server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:  # type: ignore[type-arg]
    import json

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
        results = list_chats(
            db,
            tag=args.get("tag"),
            include_untagged=args.get("include_untagged", False),
        )
        return [r.model_dump() for r in results]

    elif name == "get_chat_summary":
        results = get_chat_summary(
            db,
            chat_id=args["chat_id"],
            period=args.get("period", "week"),
            periods_back=args.get("periods_back", 1),
        )
        return [r.model_dump() for r in results]

    elif name == "get_commitments":
        results = get_commitments(
            db,
            status=args.get("status"),
            owner=args.get("owner"),
            chat_id=args.get("chat_id"),
            overdue_only=args.get("overdue_only", False),
        )
        return [r.model_dump() for r in results]

    elif name == "get_signals":
        date_from = date.fromisoformat(args["date_from"]) if args.get("date_from") else None
        results = get_signals(
            db,
            signal_types=args.get("signal_types"),
            min_strength=args.get("min_strength", 1),
            date_from=date_from,
            chat_ids=args.get("chat_ids"),
        )
        return [r.model_dump() for r in results]

    elif name == "get_relationship_state":
        results = get_relationship_state(
            db,
            chat_id=args.get("chat_id"),
        )
        return [r.model_dump() for r in results]

    elif name == "get_recent_brief":
        date_filter = date.fromisoformat(args["date"]) if args.get("date") else None
        result = get_recent_brief(db, date_filter=date_filter)
        return result.model_dump()

    else:
        raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# FastAPI app (health + auth middleware)
# ---------------------------------------------------------------------------

fastapi_app = FastAPI(title="TBC MCP Server", version="0.1.0")
fastapi_app.add_middleware(BearerTokenMiddleware)


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


async def handle_mcp(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
    await session_manager.handle_request(scope, receive, send)


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):  # type: ignore[no-untyped-def]
    log.info("Starting TBC MCP Server")
    async with session_manager.run():
        yield
    log.info("TBC MCP Server stopped")


app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/mcp", handle_mcp, methods=["GET", "POST", "DELETE"]),
        Mount("/mcp/", app=handle_mcp),
        Mount("/", app=fastapi_app),
    ],
)

# Attach the bearer token middleware at the top level so /mcp is also protected

app.add_middleware(BearerTokenMiddleware)


if __name__ == "__main__":
    uvicorn.run(
        "tbc_mcp_server.main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
