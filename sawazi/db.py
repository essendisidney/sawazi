"""Database setup. SQLite for local dev; set SAWAZI_DB_URL to a PostgreSQL URL in production,
e.g. postgresql+psycopg://sawazi:...@host:5432/sawazi. The schema is managed by Alembic (migrations/).

On Vercel (serverless) connect through Supabase's transaction pooler (port 6543): every function instance opens
its own short connections, the pooler shares a few real ones. That pooler cannot keep prepared statements, so
they are switched off, and the app does not keep its own pool."""
import os
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

ON_VERCEL = bool(os.getenv("VERCEL"))


def normalise_url(url: str) -> str:
    """Accept the postgres:// and postgresql:// forms that Supabase and Vercel hand out."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


DB_URL = normalise_url(os.getenv("SAWAZI_DB_URL", "sqlite:///./sawazi.db"))
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _engine_args(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    args = {"pool_pre_ping": True}  # managed Postgres drops idle connections
    if ON_VERCEL or ":6543/" in url:
        args["poolclass"] = NullPool
        args["connect_args"] = {"prepare_threshold": None}
    return args


engine = create_engine(DB_URL, **_engine_args(DB_URL))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# Supabase publishes the public schema through its Data API to anyone holding the project's public "anon" key.
# Sawazi never uses that API, so its tables get row level security with no policies and those roles lose every
# privilege: only Sawazi's own database login (the table owner) can read or write. Runs after every migration
# (migrations/env.py); does nothing on a PostgreSQL without Supabase's roles. Safe to run any number of times.
SUPABASE_LOCKDOWN = """
DO $$
DECLARE t record;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')
     AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
      EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.tablename);
      EXECUTE format('REVOKE ALL ON public.%I FROM anon, authenticated', t.tablename);
    END LOOP;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
    REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM anon, authenticated;
  END IF;
END $$;
"""


class Base(DeclarativeBase):
    pass


class UnmigratedDatabase(RuntimeError):
    pass


def migrate(bind=None) -> None:
    """Bring the database to the latest migration (same as `alembic upgrade head`)."""
    from alembic import command
    from alembic.config import Config

    from . import models  # noqa: F401  (register tables)

    eng = bind or engine
    cfg = Config(str(ALEMBIC_INI))
    with eng.begin() as conn:
        tables = set(inspect(conn).get_table_names())
        if tables and "alembic_version" not in tables:
            raise UnmigratedDatabase(
                f"{eng.url.render_as_string(hide_password=True)} was created before Sawazi used migrations, so its "
                "schema cannot be trusted. For local fictional data, delete the file and start again. For anything "
                "else, compare it with the models first; only if it matches exactly run `alembic stamp head`.")
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def schema_is_current(bind=None) -> bool:
    """True when the database is at the latest migration. On Vercel the app never migrates itself."""
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
    with (bind or engine).connect() as conn:
        return MigrationContext.configure(conn).get_current_revision() == head


def init_db(bind=None):
    """Migrate at start-up, except where several short-lived instances start at once (Vercel): there,
    `alembic upgrade head` runs once per release, before the deploy goes live."""
    if ON_VERCEL or os.getenv("SAWAZI_MIGRATE_ON_START", "1") == "0":
        return
    migrate(bind)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
