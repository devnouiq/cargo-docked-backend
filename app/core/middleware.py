"""Cross-cutting HTTP middleware: request ID propagation + timing.

Every response carries the same `X-Request-ID` a caller sent (or a freshly
minted one) so a customer reporting "request X failed" can be grepped
straight out of logs, and the existing `X-Process-Time` timing header
(carried over from the old inline middleware in app/main.py) for quick
latency spot-checks without a metrics backend.
"""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .logging import set_request_id

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id
        set_request_id(request_id)

        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers["X-Process-Time"] = f"{elapsed:.4f}"
        return response
