"""Alembic environment: Sawazi's own engine and models are the source of truth."""
from alembic import context
from sqlalchemy import engine_from_config, pool

from sawazi import models  # noqa: F401  (registers every table on Base.metadata)
from sawazi.db import DB_URL, Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    # Tests and tools may pass a URL explicitly; otherwise use SAWAZI_DB_URL like the app does.
    return config.attributes.get("url") or config.get_main_option("sqlalchemy.url") or DB_URL


def _opts(url: str) -> dict:
    return {
        "target_metadata": target_metadata,
        "compare_type": True,
        "render_as_batch": url.startswith("sqlite"),  # SQLite can only alter tables by copying them
    }


def run_offline() -> None:
    url = _url()
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"}, **_opts(url))
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # handed in by sawazi.db.migrate()
        context.configure(connection=connection, **_opts(str(connection.engine.url)))
        with context.begin_transaction():
            context.run_migrations()
        return
    url = _url()
    engine = engine_from_config({"sqlalchemy.url": url}, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn, **_opts(url))
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
