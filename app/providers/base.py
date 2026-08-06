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


class TrackingProvider(Protocol):
    name: str

    def supports(self, container_number: str) -> bool:
        """Whether this provider is worth trying for this number (e.g. the
        Romeu adapter only claims ROMU-prefixed numbers). Providers with no
        such restriction return True unconditionally."""
        ...

    async def track(self, container_number: str) -> NormalizedTrackingResult: ...
