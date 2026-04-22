"""Root conftest for worker-commitments tests."""
from __future__ import annotations

import sys
import os
import sqlalchemy.types as sqltypes
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.dialects.postgresql import JSONB

# Ensure tbc_worker_commitments is importable
sys.path.insert(0, os.path.dirname(__file__))

import pytest
from sqlalchemy.orm import sessionmaker


def make_sqlite_engine():
    """Create a SQLite in-memory engine with all tbc_common tables."""
    from tbc_common.db.models import Base

    engine = create_engine("sqlite:///:memory:", future=True)

    patches = []
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, JSONB):
                patches.append((col, col.type, sqltypes.JSON()))
            elif col.type.__class__.__name__ == "Vector":
                patches.append((col, col.type, sqltypes.Text()))
            elif isinstance(col.type, BigInteger) and col.primary_key and col.autoincrement:
                patches.append((col, col.type, Integer()))

    for col, _orig, new_type in patches:
        col.type = new_type

    Base.metadata.create_all(engine)

    for col, orig, _new in patches:
        col.type = orig

    return engine


@pytest.fixture()
def session():
    engine = make_sqlite_engine()
    SM = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with SM() as s:
        yield s
