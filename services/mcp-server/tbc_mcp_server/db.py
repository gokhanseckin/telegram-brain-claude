"""Database session dependency for FastAPI."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy.orm import Session
from tbc_common.db.session import get_sessionmaker


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a SQLAlchemy session."""
    session_factory = get_sessionmaker()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()
