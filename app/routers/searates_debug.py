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

Payload split (product request: one endpoint shouldn't return the entire
scraped dataset): `track_searates` used to return GoCometTracker._parse()'s
whole flattened dict (status/carrier/route summary + the full event
timeline + internal scrape timing) as an untyped `dict`, so there was no
real OpenAPI/Swagger schema for it either. Now typed via
`schemas/tracking_preview.py`: `track_searates` returns just the summary,
`track_searates_events` (new, same URL prefix + `/events`) returns the
timeline, and internal timing fields are dropped from both client-facing
responses entirely. URL paths for the two pre-existing routes are
unchanged - only the response shape/typing changed.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..providers.gocomet_http import CarrierNotResolved, RateLimited
from ..providers.gocomet_pool import get_pool
from ..providers.searates_browser import scrape_searates
from ..schemas import BulkTrackRequest
from ..schemas.tracking_preview import (
    TrackingBulkItemOut,
    TrackingBulkResponseOut,
    TrackingEventOut,
    TrackingEventsOut,
    TrackingSummaryOut,
)
from ..services.bulk_tracking_service import track_many_parallel, track_with_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["searates-debug"])

# Defense in depth for the single-lookup route: GoCometTracker.track()'s own
# retry loop is bounded by max_rotations (not by this), but a synchronous
# HTTP route still needs its own hard ceiling so a pathological run of
# rotations can't leave a request hanging for minutes with no bound of its
# own - observed live under concurrent load: some attempts took 80-90s
# before this existed. Bulk (track_many_parallel) already has an equivalent
# bound (_ATTEMPT_TIMEOUT_S in bulk_tracking_service.py) - this mirrors it.
_SINGLE_LOOKUP_TIMEOUT_S = 120.0


def _tracker_proxy_kwargs() -> dict:
    return {
        "proxy": settings.oxylabs_proxy_host,
        "proxy_username": settings.oxylabs_proxy_username,
        "proxy_password": settings.oxylabs_proxy_password,
    }


async def _lookup(number: str, sealine: str) -> dict:
    """Shared fetch behind both preview routes below - GoComet create-then-
    poll on a cache miss (providers/gocomet_http.py), or a straight DB-cache
    read on a hit (services/bulk_tracking_service.py's `track_with_cache`).
    Calling this twice for the same number in quick succession (once for the
    summary route, once for the events route) costs one live GoComet lookup,
    not two - the second call hits the DB cache the first call just wrote.
    """
    pool = get_pool(_tracker_proxy_kwargs())
    tracker = pool.acquire()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(track_with_cache, tracker, number, sealine), timeout=_SINGLE_LOOKUP_TIMEOUT_S
        )
    except RateLimited as e:
        pool.discard(tracker)  # exit IP is burned - don't hand it to the next caller
        raise HTTPException(status_code=429, detail=f"GoComet rate limited: {e}")
    except CarrierNotResolved as e:
        # A legitimate "we don't know the carrier" outcome, not a server
        # error or a burned session - see CarrierNotResolved's docstring.
        # Client should retry with an explicit ?sealine=<CARRIER_CODE>.
        pool.release(tracker)
        raise HTTPException(status_code=422, detail=str(e))
    except asyncio.TimeoutError:
        pool.discard(tracker)  # don't trust a tracker that just took >120s
        raise HTTPException(
            status_code=503,
            detail=f"GoComet did not resolve {number!r} within {_SINGLE_LOOKUP_TIMEOUT_S:.0f}s - try again shortly.",
        )
    except Exception:
        pool.discard(tracker)  # unknown failure - safer to assume the session/connection is bad
        raise
    else:
        pool.release(tracker)
    return result


@router.get("/track-searates/{number}", response_model=TrackingSummaryOut)
async def track_searates(number: str, sealine: str = "AUTO"):
    """
    Container identity/status/carrier/route summary only - no event
    timeline, no internal scrape timing. See `/track-searates/{number}/events`
    for the milestone timeline, split out into its own endpoint/payload so
    this one stays small for the common case (a caller just wants to know
    whether a number resolved and its current status).
    """
    result = await _lookup(number, sealine)
    return TrackingSummaryOut(**{field: result.get(field) for field in TrackingSummaryOut.model_fields})


@router.get("/track-searates/{number}/events", response_model=TrackingEventsOut)
async def track_searates_events(number: str, sealine: str = "AUTO"):
    """
    Milestone timeline only, split out of the summary route above. Reuses
    the same cached/live lookup (`_lookup`) - fetching the summary first and
    then this doesn't pay for a second GoComet scrape.
    """
    result = await _lookup(number, sealine)
    return TrackingEventsOut(
        number=result.get("number"),
        found=bool(result.get("found")),
        events=[TrackingEventOut(**e) for e in (result.get("events") or [])],
    )


@router.post("/track-searates/bulk", response_model=TrackingBulkResponseOut)
async def track_searates_bulk(request: BulkTrackRequest):
    """
    Fans `container_numbers` out across `batch_size` concurrent
    GoCometTracker sessions (services/bulk_tracking_service.py) instead of
    one call at a time. Each worker keeps its own session/proxy connection;
    results are cached/read through the same ContainerResult DB table the
    browser-based routes use, so duplicate container numbers within a batch
    mostly hit that cache after the first live lookup - important given
    GoComet's monthly (not per-request) quota.

    Per-item results carry the same trimmed summary fields as
    `/track-searates/{number}` (no event timeline, no scrape timing) -
    fetch `/track-searates/{number}/events` per container if the timeline
    is needed for one of them.
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
    items = [
        TrackingBulkItemOut(**{field: (r or {}).get(field) for field in TrackingBulkItemOut.model_fields})
        for r in result["results"]
    ]
    return TrackingBulkResponseOut(
        total=result["total"],
        batch_size=result["batch_size"],
        total_duration_seconds=result["total_duration_seconds"],
        success_count=result["success_count"],
        error_count=result["error_count"],
        throughput_per_sec=result["throughput_per_sec"],
        results=items,
    )


@router.get("/track-searates-browser/{number}")
async def track_searates_browser(number: str):
    """
    Debug route: loads SeaRates' tracking page in a real stealth browser
    (providers/searates_browser.py) instead of calling their JSON endpoint
    directly. Compare this against /v1/track-searates to see whether a real
    browser gets past whatever is blocking the direct API calls.
    """
    return await scrape_searates(number)
