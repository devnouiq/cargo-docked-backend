"""`GET /v1/status` - aggregate public status endpoint for the
customer-facing status page (frontend `cargo-docked-next` repo, `/status`
route). Unauthenticated and public like `/healthz`/`/readyz` in
`app/main.py`, but placed under the `/v1` prefix (unlike those two) so it
shows up in `/docs` alongside the rest of the public API - it's meant to
be linked to/polled by customers and a status-page UI, not just a load
balancer/orchestrator.

Checks:
  * `database` - identical `SELECT 1` probe to `/readyz`.
  * `redis` - pings the same process-wide arq/Redis pool the API already
    uses to enqueue webhook-delivery/scrape jobs (`workers/redis_pool.py`),
    rather than opening a second connection with its own settings.
  * `tracking` - a cheap structural signal, *not* a live call to any
    external carrier/tracking provider: whether the provider registry
    (`providers/registry.py`) has at least one provider registered.
    Building the registry only constructs adapter objects - no network
    I/O happens until `.track()` is called on a specific container, so
    this is safe to do on every status-page poll. Real per-provider
    health (SeaRates/Romeu/etc) is intentionally not queried synchronously
    here - hitting those endpoints on every poll would be slow and could
    itself trigger rate limits on the very providers it's checking.
    Individual provider identifiers are never exposed in this response -
    only the aggregate "tracking" label. Per the ongoing effort to keep
    internal scraper/provider names off customer-visible surfaces, do not
    add per-provider detail to this endpoint.

Reports degraded/down via the response body (HTTP 200 always), matching
the existing `/readyz` convention, so a JSON-parsing frontend never has to
special-case a non-2xx status code just to read the body.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import text

from ...db.session import engine
from ...providers.registry import build_default_registry
from ...schemas.status import CheckResult, StatusResponse
from ...workers.redis_pool import get_arq_pool

router = APIRouter(prefix="/v1/status", tags=["status"])


def _check_database() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - a health probe must never itself raise
        return False


async def _check_redis() -> bool:
    try:
        pool = await get_arq_pool()
        await pool.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def _check_tracking() -> bool:
    try:
        registry = build_default_registry()
        return len(registry.provider_names) > 0
    except Exception:  # noqa: BLE001
        return False


@router.get("", response_model=StatusResponse)
async def get_status() -> StatusResponse:
    database_ok = _check_database()
    redis_ok = await _check_redis()
    tracking_ok = _check_tracking()

    if database_ok and redis_ok and tracking_ok:
        overall = "ok"
    elif not database_ok:
        # The DB is a hard dependency for essentially every route; treat
        # it as a full outage rather than "degraded".
        overall = "down"
    else:
        overall = "degraded"

    return StatusResponse(
        status=overall,
        checks={
            "database": CheckResult(ok=database_ok),
            "redis": CheckResult(ok=redis_ok),
            "tracking": CheckResult(ok=tracking_ok),
        },
        checked_at=datetime.now(timezone.utc),
    )
