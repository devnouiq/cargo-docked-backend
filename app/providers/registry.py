"""Provider registry: the adapter-pattern seam between the standardized
`/v1/containers` API (services/container_service.py) and however many
carrier/terminal data sources actually exist underneath.

The default registry is API-only: searates_http (broad HTTP coverage) and
romeu_http (Romeu's own API, ROMU-prefixed numbers only), each wrapped by
a small adapter below that normalizes its native response shape into
`NormalizedTrackingResult` (providers/base.py). The two browser-automation
providers (track_trace_browser, searates_browser) are NOT wired into
`build_default_registry()` - a real headless browser per lookup is much
slower than an HTTP call and this product deliberately relies on
API-based automation only for live tracking. Their adapter classes stay
importable below for the internal debug router
(routers/searates_debug.py, `/v1/track-searates-browser/*`) and for
manual comparison/diagnostic use - just not part of the customer-facing
lookup path.

Adding a paid aggregator later (Terminal49, Vizion, project44) means
writing one more adapter class with a `track()` method in this same shape
and appending it to `build_default_registry()` - nothing else in the app
needs to change, since routers/services only ever talk to
`ProviderRegistry.track()`.

Providers are tried in order; the first one that returns `ok=True` wins.
Order is cost/reliability driven: cheap + narrow (Romeu, only claims its
own ROMU prefix) first, then the broad HTTP-based SeaRates client.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone

from ..core.config import settings
from .base import NormalizedEvent, NormalizedTrackingResult, TrackingProvider
from .romeu_http import RomeuShippingTracker, RomeuTrackerConfig
from .searates_browser import scrape_searates
from .searates_http import RateLimited, SeaRatesTracker, TrackerConfig
from .track_trace_browser import scrape_container

logger = logging.getLogger(__name__)

_FAILURE_STATUS_MARKERS = (
    "failed", "fetcher error", "error:", "blocked", "no carrier", "invalid", "no tracking",
)


def _looks_like_failure(status: str | None) -> bool:
    if not status:
        return True
    lowered = status.lower()
    return any(marker in lowered for marker in _FAILURE_STATUS_MARKERS)


def _parse_date(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _tracker_proxy_kwargs() -> dict:
    return {
        "proxy": settings.oxylabs_proxy_host,
        "proxy_username": settings.oxylabs_proxy_username,
        "proxy_password": settings.oxylabs_proxy_password,
    }


class SearatesHttpProvider:
    """Adapts providers/searates_http.py (untouched) - the fast, TLS-impersonated
    HTTP client that is this project's primary tracking source.

    One `SeaRatesTracker` per worker thread, reused across calls, instead of
    a fresh one every time. A fresh tracker means a fresh `Session` with no
    cookie/token yet, which costs ~4-5s of unavoidable setup per lookup
    (page-load-for-a-cookie + platform-token mint - see searates_http.py's
    `_ensure_session`/`_ensure_token`, both no-ops once already warm) and
    opens a brand-new proxy connection every single call, which is also
    where the occasional ~30s Oxylabs connection timeout was landing (any
    call could draw it, not just the first). Reusing a tracker means only
    the first call on a given thread pays that cost; every later call on
    the same thread reuses the still-valid cookie/JWT and skips straight to
    the data fetch.

    Deliberately thread-local (`threading.local`), not one tracker shared
    process-wide: `SeaRatesTracker`'s session/cookie/token state isn't safe
    for two requests to mutate concurrently (the request-timing route
    handlers this feeds run in Starlette's threadpool, so two lookups can
    genuinely be in flight on different threads at once). Thread-local
    means each worker thread gets its own private tracker - reused across
    that thread's calls, never touched by another thread.

    Both picking this thread's pooled tracker (`_get_tracker`) and using it
    (`tracker.track`) happen inside the SAME `asyncio.to_thread`-dispatched
    call (`_track_sync`) - not `_get_tracker` on the async event-loop thread
    and the blocking call on a worker thread. There is exactly one asyncio
    event loop (one thread) per process, so calling `_get_tracker` from the
    coroutine itself would always resolve the same `threading.local` storage
    regardless of which worker thread the blocking call actually lands on -
    silently turning "one tracker per worker thread" into "one tracker
    shared by every concurrent request", exactly the unsafe-concurrent-reuse
    case this class exists to avoid.

    Self-healing: `_track_sync` drops this thread's pooled tracker on any
    failure (network error, a stale/invalidated cookie SeaRates silently
    rejects, a rate-limit budget exhausted after rotating sessions
    internally, ...) so the next call on that thread builds a fresh
    session/token instead of repeating the same failure indefinitely. A
    normal negative result (e.g. `WRONG_NUMBER`) is not a failure here - the
    session worked fine, it just doesn't cover that container - so it does
    not drop the tracker.

    Token refresh needs no handling here: `SeaRatesTracker._ensure_token`
    already re-mints it automatically (a single ~0.5s HTTP call, not a full
    session rebuild) whenever it's near its `exp` claim - see
    searates_http.py, untouched.
    """

    name = "searates_http"

    # Bounds one attempt even if the pooled tracker's own internal retry gets
    # stuck (SeaRatesTracker retries a failing session for up to
    # max_wait_total_s=3h - fine for a background job, not for a customer
    # HTTP request). Matches SeaRatesTracker's own ~30s curl connect timeout
    # (searates_http.py, untouched) - comfortably covers even a slow-but-real
    # response on a healthy connection (observed live up to ~22-27s); a
    # genuinely stuck connection is about to hit its own internal timeout
    # right around here anyway.
    _ATTEMPT_TIMEOUT_S = 30.0

    # Hedged request, not "wait for a failure before trying anything else":
    # if the first attempt hasn't answered within this long, fire a second,
    # independent attempt in parallel (asyncio.to_thread lands it on a
    # different, currently-idle worker thread - a different underlying
    # connection/IP for free) and race them - whichever answers first wins.
    # Set well above typical latency (observed: median ~2s, 84% under 5s) so
    # normal calls never trigger it at all - only the slower tail does, and
    # for those it turns "wait up to 30-45s to find out a connection is bad"
    # into "a good connection usually already has the answer within a few
    # seconds of the hedge firing". Worst case (both attempts genuinely
    # stuck) drops too, since the two attempts now mostly overlap in time:
    # ~_HEDGE_DELAY_S + _ATTEMPT_TIMEOUT_S instead of the old sequential
    # _ATTEMPT_TIMEOUT_S + _ATTEMPT_TIMEOUT_S.
    _HEDGE_DELAY_S = 6.0

    def __init__(self) -> None:
        self._local = threading.local()

    def supports(self, container_number: str) -> bool:
        return True

    def _get_tracker(self) -> SeaRatesTracker:
        tracker = getattr(self._local, "tracker", None)
        if tracker is None:
            tracker = SeaRatesTracker(TrackerConfig(**_tracker_proxy_kwargs()))
            self._local.tracker = tracker
        return tracker

    def _track_sync(self, container_number: str) -> dict:
        tracker = self._get_tracker()
        try:
            return tracker.track(container_number)
        except Exception:
            self._local.tracker = None
            raise

    def _attempt(self, container_number: str) -> asyncio.Task:
        return asyncio.ensure_future(
            asyncio.wait_for(asyncio.to_thread(self._track_sync, container_number), timeout=self._ATTEMPT_TIMEOUT_S)
        )

    async def track(self, container_number: str) -> NormalizedTrackingResult:
        task_a = self._attempt(container_number)
        done, _ = await asyncio.wait({task_a}, timeout=self._HEDGE_DELAY_S)

        pending = {task_a}
        if task_a not in done:
            pending.add(self._attempt(container_number))

        last_error = "unknown error"
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    raw = task.result()
                except asyncio.TimeoutError:
                    last_error = f"searates_http timed out after {self._ATTEMPT_TIMEOUT_S:.0f}s"
                except RateLimited as exc:
                    last_error = f"rate_limited: {exc}"
                except Exception as exc:  # noqa: BLE001 - one provider failing must not break the registry
                    last_error = str(exc)
                else:
                    # First usable answer wins - the other attempt (if any)
                    # is still running on its own thread and will finish on
                    # its own; cancelling here just stops us from waiting on
                    # it further, same as the old design's abandoned-thread
                    # behavior.
                    for other in pending:
                        other.cancel()
                    return self._adapt(raw)
        return NormalizedTrackingResult(ok=False, error=last_error)

    @staticmethod
    def _adapt(raw: dict) -> NormalizedTrackingResult:
        containers = raw.get("containers") or []
        if not containers:
            return NormalizedTrackingResult(ok=False, error=raw.get("message") or "no container data returned", raw_data=raw)

        primary = containers[0]
        events = [
            NormalizedEvent(
                event_code=str(e.get("status") or "unknown").strip().lower().replace(" ", "_"),
                description=e.get("status"),
                location=e.get("location"),
                vessel=e.get("vessel"),
                voyage=e.get("voyage"),
                occurred_at=_parse_date(e.get("date")),
                actual=bool(e.get("actual")),
            )
            for e in primary.get("events") or []
        ]
        locations = raw.get("locations") or []
        last_location = None
        if locations:
            tail = locations[-1]
            last_location = tail.get("name") if isinstance(tail, dict) else str(tail)
        latest_event = events[-1] if events else None

        return NormalizedTrackingResult(
            ok=True,
            status=raw.get("shipment_status") or primary.get("status"),
            location=last_location,
            vessel=latest_event.vessel if latest_event else None,
            voyage=latest_event.voyage if latest_event else None,
            events=events,
            raw_data=raw,
        )


class RomeuHttpProvider:
    """Adapts providers/romeu_http.py (untouched) - Romeu Shipping's own API,
    only relevant for containers it operates directly (ROMU prefix)."""

    name = "romeu_http"

    def supports(self, container_number: str) -> bool:
        return container_number.strip().upper().startswith("ROMU")

    async def track(self, container_number: str) -> NormalizedTrackingResult:
        tracker = RomeuShippingTracker(RomeuTrackerConfig(**_tracker_proxy_kwargs()))
        try:
            raw = await asyncio.to_thread(tracker.track, container_number)
        except Exception as exc:  # noqa: BLE001
            return NormalizedTrackingResult(ok=False, error=str(exc))
        return self._adapt(raw)

    @staticmethod
    def _adapt(raw: dict) -> NormalizedTrackingResult:
        containers = raw.get("containers") or []
        if raw.get("status") != "success" or not containers:
            return NormalizedTrackingResult(ok=False, error="; ".join(raw.get("messages") or []) or "not found", raw_data=raw)

        primary = containers[0]
        events = [
            NormalizedEvent(
                event_code=str(m.get("code") or m.get("description") or "unknown").strip().lower().replace(" ", "_"),
                description=m.get("description"),
                location=m.get("port"),
                occurred_at=_parse_date(m.get("date")),
                actual=True,  # Romeu only reports movements that already happened
            )
            for m in primary.get("movements") or []
        ]
        return NormalizedTrackingResult(
            ok=True,
            status=primary.get("status") or primary.get("last_movement"),
            location=events[0].location if events else None,
            events=events,
            raw_data=raw,
        )


class TrackTraceBrowserProvider:
    """Adapts providers/track_trace_browser.py (untouched) - broad carrier
    coverage via a real browser, no structured event timeline."""

    name = "track_trace_browser"

    def supports(self, container_number: str) -> bool:
        return True

    async def track(self, container_number: str) -> NormalizedTrackingResult:
        try:
            raw = await scrape_container(container_number)
        except Exception as exc:  # noqa: BLE001
            return NormalizedTrackingResult(ok=False, error=str(exc))

        status = raw.get("status")
        if _looks_like_failure(status):
            return NormalizedTrackingResult(ok=False, error=status, raw_data=raw.get("raw_data") or {})

        location = raw.get("location")
        return NormalizedTrackingResult(
            ok=True,
            status=status,
            location=None if location == "Unknown" else location,
            raw_data=raw.get("raw_data") or {},
        )


class SearatesBrowserProvider:
    """Adapts providers/searates_browser.py (untouched) - SeaRates' public
    tracking page via a real browser; last-resort/diagnostic fallback."""

    name = "searates_browser"

    def supports(self, container_number: str) -> bool:
        return True

    async def track(self, container_number: str) -> NormalizedTrackingResult:
        try:
            raw = await scrape_searates(container_number)
        except Exception as exc:  # noqa: BLE001
            return NormalizedTrackingResult(ok=False, error=str(exc))

        status = raw.get("status")
        if _looks_like_failure(status):
            return NormalizedTrackingResult(ok=False, error=status, raw_data=raw.get("raw_data") or {})

        location = raw.get("location")
        return NormalizedTrackingResult(
            ok=True,
            status=status,
            location=None if location == "Unknown" else location,
            raw_data=raw.get("raw_data") or {},
        )


class ProviderRegistry:
    def __init__(self, providers: list[TrackingProvider]):
        self._providers = providers

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self._providers]

    async def track(self, container_number: str) -> NormalizedTrackingResult:
        attempted: list[str] = []
        for provider in self._providers:
            if not provider.supports(container_number):
                continue
            attempted.append(provider.name)
            try:
                result = await provider.track(container_number)
            except Exception:  # noqa: BLE001 - one provider's bug must not sink the whole lookup
                logger.exception("provider %s raised while tracking %s", provider.name, container_number)
                continue
            if result.ok:
                result.provider_name = provider.name
                logger.info("provider %s resolved %s", provider.name, container_number)
                return result
            logger.info("provider %s missed %s: %s", provider.name, container_number, result.error)

        # Customer-safe message only - it must never name which internal
        # providers were tried (that's an internal implementation detail,
        # not something a customer integration should see or depend on).
        # The attempted-providers list is still available server-side via
        # the "provider %s missed" log lines just above, for debugging.
        logger.info("no provider resolved %s (tried: %s)", container_number, ", ".join(attempted) or "none")
        return NormalizedTrackingResult(
            ok=False,
            error="Container data is not yet available. Try again later or verify the container number is correct.",
        )


_default_registry: ProviderRegistry | None = None


def build_default_registry() -> ProviderRegistry:
    # API-only: no browser-automation providers in the live lookup path -
    # see module docstring. TrackTraceBrowserProvider/SearatesBrowserProvider
    # stay defined above for the internal debug router only.
    #
    # Cached at module scope, not rebuilt on every call: SearatesHttpProvider
    # pools one SeaRatesTracker per worker thread (see its own docstring), so
    # every ContainerService() that ends up here - whether the API routers'
    # long-lived singleton or a fresh one built per arq job
    # (workers/tasks/scrape.py, deliberately fresh per job for test
    # isolation) - shares the same warmed-up pool instead of each getting
    # its own empty one. Safe across concurrent requests/jobs in the same
    # process for the same reason SearatesHttpProvider itself is: it holds
    # no per-call state of its own, only routes to provider instances that
    # already isolate per-thread. Tests never see this cache - they
    # monkeypatch this whole function to return a fresh fake per test (see
    # conftest.py's `_fake_provider_registry`), which fully replaces this
    # module-level function/cache for the duration of that test.
    global _default_registry
    if _default_registry is None:
        _default_registry = ProviderRegistry(
            [
                RomeuHttpProvider(),
                SearatesHttpProvider(),
            ]
        )
    return _default_registry
