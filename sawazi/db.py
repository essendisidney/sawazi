"""Database setup. SQLite for local dev; set SAWAZI_DB_URL to a PostgreSQL URL in production."""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_URL = os.getenv("SAWAZI_DB_URL", "sqlite:///./sawazi.db")

engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db(bind=None):
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(bind=bind or engine)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
