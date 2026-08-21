"""App factory: wires together every router, middleware, and lifespan
concern. Router registration order matters only for OpenAPI grouping, not
behavior - each router owns its own path prefix.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .core.config import settings
from .core.errors import register_exception_handlers
from .core.logging import configure_logging
from .core.middleware import RequestContextMiddleware
from .providers.browser_session import close_scraper_session
from .routers import searates_debug, tracking
from .routers.v1 import v1_router
from .services import container_service
from .workers.redis_pool import close_arq_pool

configure_logging(settings.log_level)

# This process's reserved slice of Oxylabs' account-wide concurrent-
# connection limit (~18-20 total, measured live - see
# SearatesHttpProvider.configure_concurrency for why this is a static
# per-process split rather than a shared/coordinated limiter). The API
# process and the arq worker process (workers/arq_app.py, its own slice:
# 12) each get a fixed, non-overlapping budget, so a customer's single
# lookup can never be queued behind the worker's bulk background scraping -
# they're different processes spending from different budgets, not
# competing for the same pool. 6 + 12 = 18, safely under the observed
# ceiling with margin.
_MAX_CONCURRENT_SEARATES_CONNECTIONS = 6

# How many of this process's connections to warm up proactively at startup,
# so early real requests land on an already-warm one instead of each
# individually paying the cold session/token setup cost. Capped at this
# process's own concurrency budget above - warming goes through the same
# semaphore as real traffic, so warming more than the budget would just
# queue instead of actually running any faster.
_WARM_POOL_SIZE = _MAX_CONCURRENT_SEARATES_CONNECTIONS

# registry.py's SearatesHttpProvider uses asyncio.to_thread for its live
# lookups, which shares the event loop's *default* executor - Python sizes
# that to min(32, cpu_count()+4) threads by default (20 on a 16-core box).
# Many concurrent single-container lookups (customers hitting /v1/containers
# or /v1/containers/{number} at once) would silently queue behind that cap
# instead of actually running in parallel. Sized generously above what this
# process needs (bulk's own concurrency is the worker process's concern -
# see workers/arq_app.py for the identical reasoning there) since a hedged
# lookup can occupy two threads at once.
_THREAD_POOL_SIZE = 60


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=_THREAD_POOL_SIZE, thread_name_prefix="tracking-lookup")
    )
    # Fire-and-forget, not awaited: warming is a handful of real network
    # round-trips (a few seconds each) and must never delay the process
    # becoming ready to serve traffic. ProviderRegistry.warm() already
    # catches per-thread failures internally, so this task itself never
    # raises - the reference just needs to be held so it isn't GC'd early.
    #
    # Goes through container_service.build_default_registry() (module
    # attribute access, not a direct import of the function) rather than
    # calling providers.registry.build_default_registry() itself - this is
    # the exact name tests/conftest.py's `_fake_provider_registry` fixture
    # monkeypatches so that "no live network calls in tests" (see
    # CLAUDE.md) holds for every ContainerService instance; going around it
    # here would mean this startup warm-up fires real requests against
    # SeaRates on every test that spins up a TestClient.
    registry = container_service.build_default_registry()
    registry.configure_concurrency(_MAX_CONCURRENT_SEARATES_CONNECTIONS)
    app.state.warm_task = asyncio.create_task(registry.warm(_WARM_POOL_SIZE))
    yield
    await close_scraper_session()
    await close_arq_pool()


def create_app() -> FastAPI:
    app = FastAPI(
        title="CargoTrack API",
        description="Real-time ocean container tracking, webhooks, and usage-based billing.",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(v1_router)
    app.include_router(tracking.router)  # deprecated /v1/track* aliases - see routers/tracking.py
    app.include_router(searates_debug.router)  # internal/debug - unchanged, see routers/searates_debug.py

    @app.get("/healthz", tags=["health"])
    def healthz() -> dict:
        """Liveness: process is up. Does not touch the DB/Redis - a slow
        dependency shouldn't take the process itself out of rotation."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    def readyz() -> dict:
        """Readiness: can this instance actually serve traffic right now."""
        from sqlalchemy import text

        from .db.session import engine

        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            db_ok = False
        return {"status": "ok" if db_ok else "degraded", "database": db_ok}

    return app


app = create_app()
