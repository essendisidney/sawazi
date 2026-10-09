"""Running on Vercel with Supabase: caller address, pooler connections, and Supabase's public API locked out."""
import pytest
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from sawazi import auth, db
from tests.conftest import TEST_DB_URL, make_engine


def _request(headers: dict, client=("10.0.0.9", 1234)) -> Request:
    return Request({"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                    "client": client})


def test_caller_address_trusts_vercel_headers_only_on_vercel(monkeypatch):
    forged = {"x-real-ip": "6.6.6.6", "x-forwarded-for": "6.6.6.6"}
    monkeypatch.delenv("VERCEL", raising=False)
    assert auth.client_ip(_request(forged)) == "10.0.0.9"  # elsewhere the proxy already resolved it
    monkeypatch.setenv("VERCEL", "1")
    assert auth.client_ip(_request({"x-real-ip": "41.90.1.2"})) == "41.90.1.2"
    assert auth.client_ip(_request({"x-forwarded-for": "41.90.1.3, 76.76.21.21"})) == "41.90.1.3"
    assert auth.client_ip(_request({})) == "10.0.0.9"


def test_supabase_connection_strings_and_pooler_settings():
    assert db.normalise_url("postgres://u:p@h:6543/postgres") == "postgresql+psycopg://u:p@h:6543/postgres"
    assert db.normalise_url("postgresql://u:p@h/x") == "postgresql+psycopg://u:p@h/x"
    assert db.normalise_url("sqlite:///./x.db") == "sqlite:///./x.db"
    pooled = db._engine_args("postgresql+psycopg://u:p@aws-0-eu-west-1.pooler.supabase.com:6543/postgres")
    assert pooled["poolclass"] is NullPool and pooled["connect_args"] == {"prepare_threshold": None}
    assert "poolclass" not in db._engine_args("postgresql+psycopg://u:p@db:5432/sawazi")


@pytest.mark.skipif(not TEST_DB_URL, reason="needs PostgreSQL (SAWAZI_TEST_DB_URL)")
def test_supabase_public_api_roles_cannot_touch_sawazi_tables():
    eng = make_engine()
    with eng.begin() as conn:  # Supabase's API roles, as on a real project
        conn.execute(text("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon NOLOGIN; END IF;"
                          " IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF; END $$"))
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT USAGE ON SCHEMA public TO anon, authenticated"))
        conn.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated"))
    db.migrate(eng)
    with eng.connect() as conn:
        tables = conn.execute(text("SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname='public'")).all()
        assert len(tables) > 20 and all(rls for _, rls in tables), [t for t, rls in tables if not rls]
        for role in ("anon", "authenticated"):
            reachable = conn.execute(text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' AND "
                "(has_table_privilege(:r, c.oid, 'SELECT') OR has_table_privilege(:r, c.oid, 'INSERT'))"),
                {"r": role}).scalars().all()
            assert reachable == [], (role, reachable)
