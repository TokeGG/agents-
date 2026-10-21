import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///:memory:")


def make_engine(url: str):
    """The Scheduler accesses the DB from several threads by design (event
    loop thread + asyncio.to_thread workers). For SQLite -- notably the
    unconfigured in-memory default, where each pooled connection would
    otherwise be its own separate empty database -- share one connection
    across threads (same pattern as tests/conftest.py). Real deploys set
    DATABASE_URL to Postgres (see .env.example) and take the plain path."""
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    return create_engine(url)


engine = make_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_session_factory():
    """Returns a callable that creates a new Session, for use by background
    tasks that must not depend on a request-scoped session's lifetime."""
    return SessionLocal
