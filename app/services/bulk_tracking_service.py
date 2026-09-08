"""Bounded-concurrency fan-out for GoCometTracker lookups.

`batch_size` workers pull container numbers off a shared queue; each worker
owns one GoCometTracker (one curl_cffi Session + proxy connection), so a
worker reused across many containers only pays session/proxy setup cost
once instead of per container. `GoCometTracker.track` itself is synchronous
(blocking sleeps for its create-then-poll cycle), so each worker's calls run
via asyncio.to_thread to avoid stalling the event loop while one worker
polls.

Caching is DB-backed (the same ContainerResult table/repository the
browser-based routes use) - duplicate container numbers within a batch
mostly hit the DB cache after the first live lookup, which is what makes it
safe to pad a batch out with repeats to validate at volume without hammering
GoComet's monthly quota repeatedly for the same number.

SQLAlchemy `Session` objects aren't thread-safe to share, so each worker
opens its own short-lived `SessionLocal()` around each individual
cache-check/persist, rather than sharing one session across threads.

Note on the `sealine` parameter name: kept as-is throughout this module
(rather than renamed to GoComet's `carrier_code`) so the frontend's existing
`/v1/track-searates/*` request shape (`searatesService.js`) needs no
changes - see app/routers/searates_debug.py's module docstring for the full
SeaRates -> GoComet swap rationale. `"AUTO"` (or empty) triggers GoComet's
own carrier auto-suggest; anything else is passed straight through as
`carrier_code` (SeaRates' sealine/SCAC-style codes mostly coincide with
GoComet's carrier codes for major carriers - not guaranteed for all, a
known best-effort limitation, not a full translation table).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ..config import settings
from ..database import SessionLocal
from ..providers.gocomet_http import GoCometTracker, RateLimited
from ..providers.gocomet_pool import GoCometSessionPool, get_pool
from ..repositories import ContainerResultRepository

logger = logging.getLogger(__name__)
_repository = ContainerResultRepository()

# Bounds one attempt even if the tracker's own internal retry gets stuck.
# Above GoCometTracker's own poll_timeout_s=80s default (config.py) so a
# normal full create+poll cycle isn't cut off mid-flight; a genuinely stuck
# attempt is about to hit its own internal timeout right around here anyway.
_ATTEMPT_TIMEOUT_S = 95.0

# Deliberately NOT hedged, unlike the old SeaRates version of this module: a
# second parallel attempt would mean a second POST create call against
# GoComet's shared *monthly* quota for no benefit, since GoComet's
# resolution time is dominated by its own server-side carrier scrape, not by
# our connection - see registry.py's GoCometHttpProvider docstring for the
# same reasoning on the standardized /v1/containers path.


async def _attempt(tracker: GoCometTracker, number: str, sealine: str) -> tuple[GoCometTracker, dict]:
    result = await asyncio.wait_for(
        asyncio.to_thread(track_with_cache, tracker, number, sealine), timeout=_ATTEMPT_TIMEOUT_S
    )
    return tracker, result


def _summarize_for_cache(result: dict) -> tuple[str, Optional[str]]:
    status = result.get("display_status") or result.get("status") or "UNKNOWN"
    location = result.get("current_location")
    return status, location


def _is_gocomet_shaped(raw_data: dict) -> bool:
    """The ContainerResult table is shared with the browser-based /v1/track
    route (providers/track_trace_browser.py) and previously held SeaRates-
    shaped rows too, neither of which carry GoCometTracker._parse()'s
    `provider` marker. A row cached by either of those for the same
    container_number would otherwise be replayed here as if it were a fresh
    GoComet result - wrong shape, silently confusing downstream. Only trust
    a cached row that actually looks like GoCometTracker output; anything
    else is treated as a miss and re-fetched live."""
    return raw_data.get("provider") == "gocomet"


def track_with_cache(tracker: GoCometTracker, number: str, sealine: str) -> dict:
    """DB cache check, then live fetch + persist on a miss. One SessionLocal
    per call - never shared across threads, so this is safe to run inside a
    worker thread (via `asyncio.to_thread`) or directly from a single-number
    route."""
    with SessionLocal() as db:
        cached = _repository.get_cached(db, number, settings.container_cache_ttl_seconds)
        if cached is not None and cached.raw_data and _is_gocomet_shaped(cached.raw_data):
            result = dict(cached.raw_data)
            result["_db_cache_hit"] = True
            return result

        carrier_code = sealine.strip().upper() if sealine and sealine.strip().upper() != "AUTO" else None
        result = tracker.track(number, carrier_code=carrier_code)
        status, location = _summarize_for_cache(result)
        _repository.upsert(db, number, status=status, location=location, raw_data=result)
        return result


async def _worker(
    worker_id: int,
    queue: "asyncio.Queue[tuple[int, str]]",
    results: list[Optional[dict]],
    sealine: str,
    pool: GoCometSessionPool,
) -> None:
    # Pulled from the pool, not built cold - the whole point of pre-warming
    # is that `batch_size` workers starting at once don't all cold-start a
    # proxy connection simultaneously (observed live: real connection
    # strain under exactly this pattern before pooling existed).
    tracker = pool.acquire()
    try:
        while True:
            try:
                index, number = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            start = time.perf_counter()
            result = None
            error_message = "unknown error"
            try:
                _, result = await _attempt(tracker, number, sealine)
            except asyncio.TimeoutError:
                error_message = f"TIMED_OUT_AFTER_{int(_ATTEMPT_TIMEOUT_S)}S"
            except RateLimited as exc:
                error_message = f"RATE_LIMITED_GAVE_UP: {exc}"
            except Exception as exc:  # noqa: BLE001 - isolate one failure from the rest of the batch
                error_message = str(exc)

            if result is None:
                # Attempt failed (timeout/rate-limit/other) - don't keep
                # using a tracker that just proved broken for this worker's
                # remaining queue items. discard() triggers a background
                # rebuild and hands back whatever's already warmed instead
                # of cold-building inline (same self-healing intent as
                # registry.py's GoCometHttpProvider._track_sync, now via
                # the pool).
                pool.discard(tracker)
                tracker = pool.acquire()

            duration = round(time.perf_counter() - start, 3)
            if result is not None:
                result["duration_seconds"] = duration
                logger.info(
                    "[worker %d] %s -> %s%s in %ss%s",
                    worker_id, number, result.get("status"),
                    f" message={result['message']!r}" if result.get("status") == "error" and result.get("message") else "",
                    duration, " [db cache hit]" if result.get("_db_cache_hit") else "",
                )
            else:
                result = {"number": number, "status": "error", "message": error_message, "duration_seconds": duration}
                logger.error("[worker %d] %s failed after %ss: %s", worker_id, number, duration, error_message)

            results[index] = result
    finally:
        # Whether this worker's queue share ran out normally, or the task
        # was cancelled, hand the (still-good) tracker back for the *next*
        # request to reuse - the pool is a long-lived singleton shared
        # across every call to this route, not scoped to one batch.
        pool.release(tracker)


async def track_many_parallel(
    numbers: list[str],
    sealine: str = "AUTO",
    batch_size: int = 10,
    **tracker_kwargs: Any,
) -> dict[str, Any]:
    """Track `numbers` using up to `batch_size` concurrent worker sessions.

    Returns per-item results plus batch-level timing/throughput so callers
    can compare this against sequential single-item timing.
    """
    if not numbers:
        return {
            "total": 0, "batch_size": batch_size, "total_duration_seconds": 0.0,
            "success_count": 0, "error_count": 0, "throughput_per_sec": 0.0, "results": [],
        }

    queue: "asyncio.Queue[tuple[int, str]]" = asyncio.Queue()
    for i, number in enumerate(numbers):
        queue.put_nowait((i, number))

    results: list[Optional[dict]] = [None] * len(numbers)
    worker_count = max(1, min(batch_size, len(numbers)))
    pool = get_pool(tracker_kwargs)

    logger.info(
        "bulk track starting: %d containers, batch_size=%d (%d workers, %d spares ready)",
        len(numbers), batch_size, worker_count, pool.pending_spares(),
    )
    start = time.perf_counter()
    workers = [
        asyncio.create_task(_worker(i, queue, results, sealine, pool))
        for i in range(worker_count)
    ]
    await asyncio.gather(*workers)
    total_duration = round(time.perf_counter() - start, 3)

    # GoComet's top-level `status` has no single fixed "success" literal the
    # way SeaRates' did - anything that isn't an explicit error/not-found/
    # still-pending outcome counts as resolved.
    success_count = sum(
        1 for r in results
        if r and r.get("status") not in ("error", "pending", "data_not_found", "invalid")
    )
    error_count = len(numbers) - success_count
    throughput = round(len(numbers) / total_duration, 2) if total_duration > 0 else 0.0

    logger.info(
        "bulk track done: %d/%d succeeded in %ss (%s req/s)",
        success_count, len(numbers), total_duration, throughput,
    )

    return {
        "total": len(numbers),
        "batch_size": batch_size,
        "total_duration_seconds": total_duration,
        "success_count": success_count,
        "error_count": error_count,
        "throughput_per_sec": throughput,
        "results": results,
    }
