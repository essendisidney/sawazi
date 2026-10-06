"""Database setup. SQLite for local dev; set SAWAZI_DB_URL to a PostgreSQL URL in production,
e.g. postgresql+psycopg://sawazi:...@host:5432/sawazi. The schema is managed by Alembic (migrations/)."""
import os
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_URL = os.getenv("SAWAZI_DB_URL", "sqlite:///./sawazi.db")
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
    pool_pre_ping=not DB_URL.startswith("sqlite"),  # managed Postgres drops idle connections
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


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


def init_db(bind=None):
    migrate(bind)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
