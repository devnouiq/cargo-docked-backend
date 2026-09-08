"""Common interface every tracking source implements, plus the normalized
result shape the provider registry (registry.py) and container repository
(repositories/containers.py) both speak - regardless of which upstream
(SeaRates' JSON API, Romeu's API, a browser-scraped carrier page) produced
it.

Browser-based providers (track_trace_browser, searates_browser) are
natively async. The HTTP-based providers (searates_http, romeu_http) are
synchronous, blocking clients - kept that way so they stay simple and
independently usable/testable outside FastAPI (including as standalone
CLI tools, and by the pre-existing debug router/services). Callers that
need a uniform `await` (registry.py's adapters) wrap the sync ones in
`asyncio.to_thread` at the call site instead of forcing a fake-async shim
into the provider classes themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class NormalizedEvent:
    event_code: str
    description: str | None = None
    location: str | None = None
    vessel: str | None = None
    voyage: str | None = None
    occurred_at: datetime | None = None
    actual: bool = False


@dataclass
class NormalizedTrackingResult:
    ok: bool
    status: str | None = None
    location: str | None = None
    vessel: str | None = None
    voyage: str | None = None
    events: list[NormalizedEvent] = field(default_factory=list)
    raw_data: dict = field(default_factory=dict)
    error: str | None = None
    provider_name: str | None = None  # set by ProviderRegistry on success
    # Upstream provider's own tracking-request id (currently only GoComet -
    # its create-then-poll flow issues one per tracking request). None for
    # providers with no such concept (SeaRates, Romeu). Persisted by
    # ContainerService onto TrackedContainer.provider_tracking_id so a later
    # refresh can resume polling instead of paying for a fresh create.
    provider_tracking_id: str | None = None


class TrackingProvider(Protocol):
    name: str

    def supports(self, container_number: str) -> bool:
        """Whether this provider is worth trying for this number (e.g. the
        Romeu adapter only claims ROMU-prefixed numbers). Providers with no
        such restriction return True unconditionally."""
        ...

    async def track(
        self,
        container_number: str,
        *,
        resume_id: str | None = None,
        on_created: object | None = None,
    ) -> NormalizedTrackingResult:
        """`resume_id`: a previously-persisted `provider_tracking_id` this
        provider issued for this container, if any - providers with no
        create-then-poll concept (SeaRates, Romeu) just ignore it.
        `on_created`: optional `Callable[[str], None]`, invoked synchronously
        the instant a *new* provider-side tracking id is obtained (before any
        polling), so the caller can persist it right away. Typed as `object`
        here (not `Callable`) to keep this Protocol import-light; concrete
        providers annotate it precisely."""
        ...
