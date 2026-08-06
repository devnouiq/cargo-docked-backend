"""App factory: wires together every router, middleware, and lifespan
concern. Router registration order matters only for OpenAPI grouping, not
behavior - each router owns its own path prefix.
"""

from __future__ import annotations

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
from .workers.redis_pool import close_arq_pool

configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
