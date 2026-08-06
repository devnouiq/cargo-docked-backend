from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

from .config import settings

# NullPool instead of the default QueuePool (5 + 10 overflow = 15 connections
# max): that ceiling was silently killing bulk-tracking workers under
# concurrency ("QueuePool limit ... connection timed out") long before
# SeaRates itself became the bottleneck. SQLite already serializes concurrent
# writers at the file level via the busy_timeout pragma below, so pooling
# on top of that just adds an artificial second ceiling with no benefit.
engine = create_engine(
    settings.database_url, connect_args={"check_same_thread": False}, poolclass=NullPool
)


@event.listens_for(engine, "connect")
def _set_sqlite_busy_timeout(dbapi_connection, connection_record):
    # SQLite allows only one writer at a time; under concurrent bulk-tracking
    # writes, a contending writer waits up to this long instead of failing
    # immediately with "database is locked".
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA busy_timeout = 5000")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
