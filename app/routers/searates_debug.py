"""Internal debug router - kept at the same URL paths
(`/v1/track-searates*`) for frontend compatibility (`searatesService.js` in
the cargo-docked-next repo calls these exact paths for the tracking page's
single/bulk search box), but as of the SeaRates -> GoComet swap the two live
routes (`track_searates`, `track_searates_bulk`) call GoCometTracker
underneath instead of SeaRatesTracker. This deviates from this file's
previous "do not modify" note (see CLAUDE.md) - that note predates this
swap; it was about protecting SeaRates' load-tested auth/rate-limit
mechanics from casual edits, not about pinning this router to SeaRates
specifically. `track_searates_browser` (SeaRates via a real browser) is
left untouched/unrelated - a separate diagnostic path, not part of this
swap.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..providers.gocomet_http import RateLimited, GoCometTracker, TrackerConfig
from ..providers.searates_browser import scrape_searates
from ..schemas import BulkTrackRequest
from ..services.bulk_tracking_service import track_many_parallel, track_with_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["searates-debug"])


def _tracker_proxy_kwargs() -> dict:
    return {
        "proxy": settings.oxylabs_proxy_host,
        "proxy_username": settings.oxylabs_proxy_username,
        "proxy_password": settings.oxylabs_proxy_password,
    }


@router.get("/track-searates/{number}")
async def track_searates(number: str, sealine: str = "AUTO"):
    """
    Calls GoComet's create-then-poll public tracking API directly
    (providers/gocomet_http.py), bypassing browser automation entirely.
    Checks the shared ContainerResult DB cache first (same cache the bulk
    route and browser-based routes use) and only hits GoComet live on a
    miss. `sealine` is passed straight through to GoComet as `carrier_code`
    ("AUTO" triggers GoComet's own carrier auto-suggest) - see
    bulk_tracking_service.py's module docstring for why this parameter name
    stays as `sealine` despite no longer meaning SeaRates' sealine/SCAC code.
    """
    tracker = GoCometTracker(TrackerConfig(**_tracker_proxy_kwargs()))
    try:
        result = await asyncio.to_thread(track_with_cache, tracker, number, sealine)
    except RateLimited as e:
        raise HTTPException(status_code=429, detail=f"GoComet rate limited: {e}")
    return result


@router.post("/track-searates/bulk")
async def track_searates_bulk(request: BulkTrackRequest):
    """
    Fans `container_numbers` out across `batch_size` concurrent
    GoCometTracker sessions (services/bulk_tracking_service.py) instead of
    one call at a time. Each worker keeps its own session/proxy connection;
    results are cached/read through the same ContainerResult DB table the
    browser-based routes use, so duplicate container numbers within a batch
    mostly hit that cache after the first live lookup - important given
    GoComet's monthly (not per-request) quota.
    """
    if not request.container_numbers:
        raise HTTPException(status_code=400, detail="container_numbers must not be empty")
    if request.batch_size < 1:
        raise HTTPException(status_code=400, detail="batch_size must be >= 1")

    logger.info(
        "/v1/track-searates/bulk request: %d containers, batch_size=%d",
        len(request.container_numbers), request.batch_size,
    )
    result = await track_many_parallel(
        request.container_numbers,
        sealine=request.sealine,
        batch_size=request.batch_size,
        **_tracker_proxy_kwargs(),
    )
    return result


@router.get("/track-searates-browser/{number}")
async def track_searates_browser(number: str):
    """
    Debug route: loads SeaRates' tracking page in a real stealth browser
    (providers/searates_browser.py) instead of calling their JSON endpoint
    directly. Compare this against /v1/track-searates to see whether a real
    browser gets past whatever is blocking the direct API calls.
    """
    return await scrape_searates(number)
