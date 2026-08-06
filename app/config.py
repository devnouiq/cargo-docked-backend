"""Backward-compatible shim over app/core/config.py.

Superseded by core/config.py's validated pydantic Settings, but kept so
pre-existing modules intentionally left untouched -
app/providers/searates_http.py, app/services/bulk_tracking_service.py,
app/services/tracking_service.py, app/routers/searates_debug.py - keep
working unchanged on `from .config import settings` /
`from ..config import settings`, reading the same settings object as the
rest of the app instead of a second, divergently-configured one.
"""

from .core.config import settings

__all__ = ["settings"]
