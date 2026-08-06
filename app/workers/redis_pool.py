"""Lazily-created, process-wide arq/Redis pool.

Shared by the API process (to *enqueue* jobs - webhook_service.py) and the
arq worker process (which gets its own pool via arq's `ctx["redis"]`, set
up from `WorkerSettings` in workers/arq_app.py). Created on first use
rather than at import time so importing this module never requires Redis
to be reachable (e.g. running the test suite with no Redis available).
"""

from __future__ import annotations

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from ..core.config import settings

_pool: ArqRedis | None = None


async def get_arq_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _pool


async def close_arq_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
