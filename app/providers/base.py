"""Common interface every tracking source implements.

Browser-based providers (track_trace_browser, searates_browser) are
natively async. The HTTP-based providers (searates_http, romeu_http) are
synchronous, blocking clients - kept that way so they stay simple and
independently usable/testable outside FastAPI (including as standalone
CLI tools). Callers that need a uniform `await` (the services layer) wrap
the sync ones in `asyncio.to_thread` at the call site instead of forcing a
fake-async shim into the provider classes themselves.
"""

from __future__ import annotations

from typing import Protocol


class TrackingProvider(Protocol):
    async def track(self, container_number: str) -> dict: ...
