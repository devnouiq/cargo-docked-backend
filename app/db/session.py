"""Engine + session factory.

Kept synchronous SQLAlchemy (matching the rest of this codebase's style,
including the scraping providers) rather than switching to an async
engine - the tradeoff is that DB-bound route handlers must be declared
`def`, not `async def`, so FastAPI/Starlette dispatches them to its
threadpool instead of blocking the event loop (see routers/ - only routes
that `await` real async I/O, like OAuth token exchange or the provider
registry, are declared `async def`).

SQLite (used by the test suite - see tests/conftest.py) needs
`check_same_thread=False` and has no real connection pool; Postgres gets a
real pool sized from settings. Branching here keeps both cases correct
without callers needing to know which engine they're on.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from ..core.config import settings


def _build_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite"):
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_busy_timeout(dbapi_connection, connection_record):  # noqa: ANN001
            # SQLite allows one writer at a time; a concurrent writer waits
            # up to this long instead of failing immediately with "database
            # is locked" (carried over from the original app/database.py).
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA busy_timeout = 5000")
            cursor.close()

        return engine

    return create_engine(
        database_url,
        poolclass=QueuePool,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_max_overflow,
        pool_pre_ping=True,
    )


engine = _build_engine(settings.database_url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
