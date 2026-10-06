"""Test database. In-memory SQLite by default; set SAWAZI_TEST_DB_URL to run the whole suite on PostgreSQL:

    SAWAZI_TEST_DB_URL=postgresql+psycopg://sawazi:...@127.0.0.1:55432/sawazi_test python -m pytest -q

The PostgreSQL database is wiped and rebuilt for every test, so never point this at real data.
"""
import os

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from sawazi import models  # noqa: F401  (register tables)
from sawazi.db import Base

TEST_DB_URL = os.getenv("SAWAZI_TEST_DB_URL")
_engines = []


def make_engine():
    """A fresh, empty database with the current schema."""
    if not TEST_DB_URL:
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(eng)
    else:
        eng = create_engine(TEST_DB_URL)
        with eng.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        Base.metadata.create_all(eng)
        # Tests insert rows with small explicit ids; start every id sequence well above them.
        with eng.begin() as conn:
            for table in inspect(conn).get_table_names():
                conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), 100000)"))
    _engines.append(eng)
    return eng


@pytest.fixture(autouse=True)
def _dispose_engines():
    yield
    while _engines:
        _engines.pop().dispose()
