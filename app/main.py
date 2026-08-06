import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from . import models
from .config import settings
from .database import engine
from .providers.browser_session import close_scraper_session
from .routers import searates_debug, tracking

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Create database tables
models.Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_scraper_session()


app = FastAPI(title="Track-Trace API", version="1.0.0", lifespan=lifespan)

app.include_router(tracking.router)
app.include_router(searates_debug.router)


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    response.headers["X-Process-Time"] = f"{elapsed:.4f}"
    return response
