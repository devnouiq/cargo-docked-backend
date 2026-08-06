"""Single place that does cache-check -> provider fetch -> persist, used by
both the single-container and batch routes in routers/tracking.py. Before
this reorg, api/main.py had this same three-step sequence written out twice
(once per route) - this is that logic, written once.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from sqlalchemy.orm import Session

from ..config import settings
from ..repositories import ContainerResultRepository

_repository = ContainerResultRepository()

TrackFn = Callable[[str], Awaitable[dict]]


async def get_or_track(db: Session, container_number: str, provider: TrackFn) -> dict:
    """Return a cached result if still fresh, otherwise fetch live via
    `provider(container_number)` and persist it.

    `provider` is any async callable of one container number returning
    `{status, location, raw_data, ...}` - e.g. providers.track_trace_browser.scrape_container.
    """
    cached = _repository.get_cached(db, container_number, settings.container_cache_ttl_seconds)
    if cached is not None:
        return {
            "container_number": cached.container_number,
            "status": cached.status,
            "location": cached.location,
            "raw_data": cached.raw_data,
            "cached": True,
            "duration_seconds": None,
        }

    scraped = await provider(container_number)
    row = _repository.upsert(
        db,
        container_number,
        status=scraped["status"],
        location=scraped.get("location"),
        raw_data=scraped.get("raw_data"),
    )
    return {
        "container_number": row.container_number,
        "status": row.status,
        "location": row.location,
        "raw_data": row.raw_data,
        "cached": False,
        "duration_seconds": scraped.get("duration_seconds"),
    }
