"""Backward-compatible shim over app/db/session.py + app/db/base.py.

Superseded by the db/ package, but kept so pre-existing modules that are
intentionally left untouched by the production-backend rework -
app/repositories.py, app/services/bulk_tracking_service.py,
app/services/tracking_service.py (and transitively
app/routers/searates_debug.py) - keep working unchanged on
`from .database import Base, SessionLocal, get_db`. There is only ever
one engine/Base/SessionLocal in this app; this module just re-exports it
under the old name so the shared ContainerResult cache table stays a
single source of truth instead of splitting onto two connections.
"""

from .db.base import Base
from .db.session import SessionLocal, engine, get_db

__all__ = ["Base", "SessionLocal", "engine", "get_db"]
