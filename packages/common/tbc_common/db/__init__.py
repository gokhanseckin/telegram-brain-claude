"""SQLAlchemy models and session management for Telegram Business Brain.

The schema here is the canonical translation of `docs/mvp-spec.md` §4.
Any schema change must ship an Alembic migration — never edit models
without a corresponding migration.
"""

from tbc_common.db.models import (
    Base,
    BriefFeedback,
    Chat,
    ChatSummary,
    Commitment,
    Message,
    MessageUnderstanding,
    RadarAlert,
    RelationshipState,
    User,
)
from tbc_common.db.session import get_engine, get_sessionmaker

__all__ = [
    "Base",
    "BriefFeedback",
    "Chat",
    "ChatSummary",
    "Commitment",
    "Message",
    "MessageUnderstanding",
    "RadarAlert",
    "RelationshipState",
    "User",
    "get_engine",
    "get_sessionmaker",
]
