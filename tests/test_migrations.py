"""Migrations must build exactly the schema the models describe, on SQLite and (with SAWAZI_TEST_DB_URL) PostgreSQL."""
from datetime import datetime

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from sawazi.db import ALEMBIC_INI, Base, UnmigratedDatabase, migrate
from sawazi.models import AuditEvent, Institution
from tests.conftest import TEST_DB_URL


@pytest.fixture()
def empty_engine(tmp_path):
    if TEST_DB_URL:
        eng = create_engine(TEST_DB_URL)
        with eng.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    else:
        eng = create_engine(f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    yield eng
    eng.dispose()


def test_migrations_match_the_models(empty_engine):
    migrate(empty_engine)
    with empty_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == [], f"models and migrations disagree; run alembic revision --autogenerate: {diff}"


def test_money_columns_are_64_bit(empty_engine):
    migrate(empty_engine)
    cols = {c["name"]: c["type"] for c in inspect(empty_engine).get_columns("loans")}
    assert "BIGINT" in str(cols["balance_cents"]).upper()
    with empty_engine.begin() as conn:  # KES 50 million: too big for a 32-bit column
        conn.execute(text("INSERT INTO institutions (name, kind) VALUES ('Big SACCO', 'sacco')"))
        conn.execute(text("INSERT INTO members (institution_id, member_no, name) VALUES (1, 'M1', 'A')"))
        conn.execute(text("""INSERT INTO loans (institution_id, member_id, loan_no, product, principal_cents, balance_cents,
            installment_cents, arrears_cents, penalty_arrears_cents, interest_arrears_cents, arrears_breakdown,
            days_in_arrears, repays_via, status) VALUES (1, 1, 'L1', 'Dev', 5000000000, 5000000000, 0, 0, 0, 0,
            false, 0, 'bank', 'active')"""))
        assert conn.execute(text("SELECT balance_cents FROM loans")).scalar() == 5_000_000_000


def test_database_refuses_to_change_audit_events(empty_engine):
    migrate(empty_engine)
    with empty_engine.begin() as conn:
        conn.execute(Institution.__table__.insert().values(id=1, name="X", kind="sacco"))
        conn.execute(AuditEvent.__table__.insert().values(institution_id=1, at=datetime(2026, 10, 7), actor_kind="user",
                                                          actor_name="A", action="test"))
    for sql in ("UPDATE audit_events SET note = 'tampered'", "DELETE FROM audit_events"):
        with pytest.raises(DBAPIError, match="append-only"):
            with empty_engine.begin() as conn:
                conn.execute(text(sql))
    with empty_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*), max(note) FROM audit_events")).one() == (1, None)


def test_downgrade_and_upgrade_again(empty_engine):
    migrate(empty_engine)
    cfg = Config(str(ALEMBIC_INI))
    with empty_engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "base")
    assert set(inspect(empty_engine).get_table_names()) <= {"alembic_version"}
    migrate(empty_engine)
    assert "audit_events" in inspect(empty_engine).get_table_names()


def test_refuses_a_database_made_before_migrations(empty_engine):
    Base.metadata.create_all(empty_engine)  # the old way: no alembic_version table
    with pytest.raises(UnmigratedDatabase, match="created before Sawazi used migrations"):
        migrate(empty_engine)


def test_migrate_twice_is_harmless(empty_engine):
    migrate(empty_engine)
    migrate(empty_engine)
    with empty_engine.connect() as conn:
        head = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == head
