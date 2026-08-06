"""Provider registry: the adapter-pattern seam between the standardized
`/v1/containers` API (services/container_service.py) and however many
carrier/terminal data sources actually exist underneath.

Today that's four scrapers (searates_http, romeu_http, track_trace_browser,
searates_browser), each wrapped by a small adapter below that normalizes
its native response shape into `NormalizedTrackingResult`
(providers/base.py). Adding a paid aggregator later (Terminal49, Vizion,
project44) means writing one more adapter class with a `track()` method in
this same shape and appending it to `build_default_registry()` - nothing
else in the app needs to change, since routers/services only ever talk to
`ProviderRegistry.track()`.

Providers are tried in order; the first one that returns `ok=True` wins.
Order is cost/reliability driven: cheap + narrow (Romeu, only claims its
own ROMU prefix) first, then the broad HTTP-based SeaRates client, then
the two browser-automation fallbacks (slow - a real headless browser
per call - but cover carriers the HTTP paths don't).
"""

from __future__ import annotations

import asyncio
import logging
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
    HTTP client that is this project's primary tracking source."""

    name = "searates_http"

    def supports(self, container_number: str) -> bool:
        return True

    async def track(self, container_number: str) -> NormalizedTrackingResult:
        # Fresh tracker per call (own session/token), matching the pattern
        # the existing debug route already uses for single lookups - a
        # SeaRatesTracker isn't designed for concurrent calls sharing one
        # instance across threads.
        tracker = SeaRatesTracker(TrackerConfig(**_tracker_proxy_kwargs()))
        try:
            raw = await asyncio.to_thread(tracker.track, container_number)
        except RateLimited as exc:
            return NormalizedTrackingResult(ok=False, error=f"rate_limited: {exc}")
        except Exception as exc:  # noqa: BLE001 - one provider failing must not break the registry
            return NormalizedTrackingResult(ok=False, error=str(exc))
        return self._adapt(raw)

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

        return NormalizedTrackingResult(
            ok=False, error=f"no provider resolved this container (tried: {', '.join(attempted) or 'none'})"
        )


def build_default_registry() -> ProviderRegistry:
    return ProviderRegistry(
        [
            RomeuHttpProvider(),
            SearatesHttpProvider(),
            TrackTraceBrowserProvider(),
            SearatesBrowserProvider(),
        ]
    )
