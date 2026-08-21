"""Bounded-concurrency fan-out for SeaRatesTracker lookups.

`batch_size` workers pull container numbers off a shared queue; each worker
owns one SeaRatesTracker (one curl_cffi Session + one cached platform token),
so a worker reused across many containers only pays the page-load/token cost
once instead of per container. `SeaRatesTracker.track` itself is synchronous
(blocking sleeps for throttle/backoff), so each worker's calls run via
asyncio.to_thread to avoid stalling the event loop while one worker backs off.

Caching is DB-backed (the same ContainerResult table/repository the
browser-based routes use), not the on-disk JSON cache the SeaRates provider
used to keep - duplicate container numbers within a batch mostly hit the DB
cache after the first live lookup, which is what makes it safe to pad a
batch out with repeats to validate at volume (e.g. 500 requests at
batch_size=50) without hammering SeaRates 500 times over.

SQLAlchemy `Session` objects aren't thread-safe to share, so each worker
opens its own short-lived `SessionLocal()` around each individual
cache-check/persist, rather than sharing one session across threads.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ..config import settings
from ..database import SessionLocal
from ..providers.searates_http import RateLimited, SeaRatesTracker, TrackerConfig
from ..repositories import ContainerResultRepository

logger = logging.getLogger(__name__)
_repository = ContainerResultRepository()

# Bounds one attempt even if the tracker's own internal retry gets stuck
# (SeaRatesTracker retries a failing session for up to max_wait_total_s=3h -
# fine for a background job in isolation, not here, where every other queued
# container behind it in this worker's share of the batch would wait too).
# Matches SeaRatesTracker's own ~30s curl connect timeout (searates_http.py,
# untouched) - comfortably covers even a slow-but-real response.
_ATTEMPT_TIMEOUT_S = 30.0

# Hedged, not "wait for a failure before trying anything else" - same
# reasoning and same value as registry.py's SearatesHttpProvider (the
# standardized /v1/containers path): if this worker's current attempt hasn't
# answered within this long, fire a second, independent attempt on a brand
# new tracker (fresh proxy connection) in parallel and use whichever answers
# first. Set well above typical per-item latency so normal items never
# trigger it - only the slow/stuck tail does. Whichever tracker actually
# wins the race becomes this worker's tracker for the *next* queue item too
# (see _worker) - if the hedge's fresh tracker won, that's a strictly better
# tracker to keep than whatever the original was stuck on.
_HEDGE_DELAY_S = 6.0


def _new_tracker(worker_id: int, tracker_kwargs: dict[str, Any]) -> SeaRatesTracker:
    return SeaRatesTracker(TrackerConfig(**tracker_kwargs, session_label=f"worker-{worker_id}"))


async def _attempt(tracker: SeaRatesTracker, number: str, sealine: str) -> tuple[SeaRatesTracker, dict]:
    result = await asyncio.wait_for(
        asyncio.to_thread(track_with_cache, tracker, number, sealine), timeout=_ATTEMPT_TIMEOUT_S
    )
    return tracker, result


def _summarize_for_cache(result: dict) -> tuple[str, Optional[str]]:
    status = result.get("shipment_status") or result.get("status") or "UNKNOWN"
    locations = result.get("locations") or []
    location = locations[-1]["name"] if locations else None
    return status, location


def _is_searates_shaped(raw_data: dict) -> bool:
    """The ContainerResult table is shared with the browser-based /v1/track
    route (providers/track_trace_browser.py), which writes a totally
    different shape (carrier_url/full_text, no `shipment_status`). A row
    cached by that route for the same container_number would otherwise be
    replayed here as if it were a SeaRates result - wrong data shape and it
    silently miscounts as an error downstream (no `status` field to match
    on). Only trust a cached row that actually looks like SeaRatesTracker
    output; anything else is treated as a miss and re-fetched live."""
    return "shipment_status" in raw_data


def track_with_cache(tracker: SeaRatesTracker, number: str, sealine: str) -> dict:
    """DB cache check, then live fetch + persist on a miss. One SessionLocal
    per call - never shared across threads, so this is safe to run inside a
    worker thread (via `asyncio.to_thread`) or directly from a single-number
    route."""
    with SessionLocal() as db:
        cached = _repository.get_cached(db, number, settings.container_cache_ttl_seconds)
        if cached is not None and cached.raw_data and _is_searates_shaped(cached.raw_data):
            result = dict(cached.raw_data)
            result["_db_cache_hit"] = True
            return result

        result = tracker.track(number, sealine)
        status, location = _summarize_for_cache(result)
        _repository.upsert(db, number, status=status, location=location, raw_data=result)
        return result


async def _worker(
    worker_id: int,
    queue: "asyncio.Queue[tuple[int, str]]",
    results: list[Optional[dict]],
    sealine: str,
    tracker_kwargs: dict[str, Any],
) -> None:
    tracker = _new_tracker(worker_id, tracker_kwargs)
    while True:
        try:
            index, number = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        start = time.perf_counter()
        task_a = asyncio.ensure_future(_attempt(tracker, number, sealine))
        done, _ = await asyncio.wait({task_a}, timeout=_HEDGE_DELAY_S)

        pending = {task_a}
        if task_a not in done:
            # This worker's current tracker hasn't answered within the hedge
            # delay - race a second, independent attempt on a brand new
            # tracker (fresh proxy connection) instead of waiting out
            # whatever's wrong with the first one.
            pending.add(asyncio.ensure_future(_attempt(_new_tracker(worker_id, tracker_kwargs), number, sealine)))

        result = None
        error_message = "unknown error"
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    winning_tracker, result = task.result()
                except asyncio.TimeoutError:
                    error_message = f"TIMED_OUT_AFTER_{int(_ATTEMPT_TIMEOUT_S)}S"
                except RateLimited as exc:
                    error_message = f"RATE_LIMITED_GAVE_UP: {exc}"
                except Exception as exc:  # noqa: BLE001 - isolate one failure from the rest of the batch
                    error_message = str(exc)
                else:
                    # First usable answer wins. Keep whichever tracker
                    # actually produced it as this worker's tracker for the
                    # *next* queue item - if the hedge's fresh tracker won,
                    # that's a strictly better one to carry forward than
                    # whatever the original was stuck on. The other attempt
                    # (if any) is left to finish on its own thread; cancel
                    # here just stops us waiting on it further.
                    tracker = winning_tracker
                    for other in pending:
                        other.cancel()
                    pending = set()
                    break

        if result is None:
            # Every attempt failed (timeout/rate-limit/other) - don't keep
            # using a tracker that just proved broken for this worker's
            # remaining queue items. Same self-healing as registry.py's
            # SearatesHttpProvider._track_sync.
            tracker = _new_tracker(worker_id, tracker_kwargs)

        duration = round(time.perf_counter() - start, 3)
        if result is not None:
            result["duration_seconds"] = duration
            logger.info(
                "[worker %d] %s -> %s (%s)%s in %ss%s",
                worker_id, number, result.get("status"), result.get("shipment_status"),
                f" message={result['message']!r}" if result.get("status") == "error" and result.get("message") else "",
                duration, " [db cache hit]" if result.get("_db_cache_hit") else "",
            )
        else:
            result = {"number": number, "status": "error", "message": error_message, "duration_seconds": duration}
            logger.error("[worker %d] %s failed after %ss: %s", worker_id, number, duration, error_message)

        results[index] = result


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

    logger.info(
        "bulk track starting: %d containers, batch_size=%d (%d workers)",
        len(numbers), batch_size, worker_count,
    )
    start = time.perf_counter()
    workers = [
        asyncio.create_task(_worker(i, queue, results, sealine, tracker_kwargs))
        for i in range(worker_count)
    ]
    await asyncio.gather(*workers)
    total_duration = round(time.perf_counter() - start, 3)

    success_count = sum(1 for r in results if r and r.get("status") == "success")
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
