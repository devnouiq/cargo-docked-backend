"""Cross-cutting HTTP middleware: request ID propagation + timing.

Every response carries the same `X-Request-ID` a caller sent (or a freshly
minted one) so a customer reporting "request X failed" can be grepped
straight out of logs, and the existing `X-Process-Time` timing header
(carried over from the old inline middleware in app/main.py) for quick
latency spot-checks without a metrics backend.
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .logging import set_request_id

logger = logging.getLogger(__name__)

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


class RateLimitHeadersMiddleware(BaseHTTPMiddleware):
    """Surfaces the org's real, existing credit-quota state as standard
    `X-RateLimit-*` headers (plus `Retry-After` on a 429), instead of the
    quota system being opaque - a customer had to hit the wall to find out
    it existed, with no way to see how close they were.

    There is no separate short-window (req/s) limiter in this API - the
    credit-per-billing-period balance IS the rate limit, so these headers
    are derived directly from it, not from a new invented mechanism.

    Reads `request.state.organization_id`, already stashed by
    `dependencies.get_api_key_principal` for every `/v1` product-API
    request - so this only ever adds headers to requests that actually
    went through API-key auth, never to public/JWT-auth routes. Opens its
    own short-lived session (same pattern as
    `container_service.py`'s `_persist_tracking_id_early`) since a
    middleware runs outside any route's `Depends(get_db)` session.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        org_id = getattr(request.state, "organization_id", None)
        if not org_id:
            return response

        try:
            self._apply_headers(response, org_id)
        except Exception:  # noqa: BLE001 - header enrichment must never break the real response
            logger.exception("RateLimitHeadersMiddleware: failed to attach headers for org %s", org_id)
        return response

    @staticmethod
    def _apply_headers(response: Response, org_id: str) -> None:
        import uuid as uuid_module

        from ..db.session import SessionLocal
        from ..models.billing import Subscription
        from ..services.usage_service import UsageService

        with SessionLocal() as db:
            balance = UsageService().get_balance(db, uuid_module.UUID(org_id))
            subscription = db.query(Subscription).filter_by(organization_id=uuid_module.UUID(org_id)).one_or_none()

        response.headers["X-RateLimit-Limit"] = str(balance.credits_included_per_period)
        response.headers["X-RateLimit-Remaining"] = str(max(balance.credits_remaining, 0))

        reset_at = subscription.current_period_end if subscription else None
        if reset_at is not None:
            response.headers["X-RateLimit-Reset"] = reset_at.isoformat()

        if response.status_code == 429:
            if reset_at is not None:
                from datetime import datetime, timezone

                # SQLite (tests) doesn't reliably round-trip
                # DateTime(timezone=True) as aware - normalize before
                # subtracting, same pattern as container_service.py's
                # _is_stale/_is_stuck.
                if reset_at.tzinfo is None:
                    reset_at = reset_at.replace(tzinfo=timezone.utc)
                seconds = max(int((reset_at - datetime.now(timezone.utc)).total_seconds()), 0)
                response.headers["Retry-After"] = str(seconds)
            # No subscription (still on the one-time free-signup grant) has
            # no renewal at all - see usage_service.py's
            # _quota_exceeded_error - so there's no honest Retry-After value
            # to give; the response body's "Upgrade your plan" message is
            # the real answer for that case, not a wait time.
