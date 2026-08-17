"""GET /v1/status - public aggregate status endpoint used by the
customer-facing status page in the frontend repo.

Covers: response shape (all-ok), degraded when a non-DB check fails,
"down" when the DB check fails, and that a raising Redis check doesn't
crash the endpoint (it's caught and reported as `ok: false`, not a 500).
No real Redis/DB-outage is needed - `app.routers.v1.status._check_redis`
and `_check_tracking` are monkeypatched directly, the same way
`conftest.py` fakes the provider registry and arq pool for the rest of
the suite.
"""

from __future__ import annotations

import app.routers.v1.status as status_router


def test_status_all_ok(client, monkeypatch):
    monkeypatch.setattr(status_router, "_check_redis", _async_true)
    monkeypatch.setattr(status_router, "_check_tracking", lambda: True)

    resp = client.get("/v1/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"] == {
        "database": {"ok": True},
        "redis": {"ok": True},
        "tracking": {"ok": True},
    }
    assert "checked_at" in body


def test_status_degraded_when_redis_down(client, monkeypatch):
    async def _fail_redis():
        return False

    monkeypatch.setattr(status_router, "_check_redis", _fail_redis)
    monkeypatch.setattr(status_router, "_check_tracking", lambda: True)

    resp = client.get("/v1/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["redis"]["ok"] is False
    assert body["checks"]["tracking"]["ok"] is True


def test_status_redis_check_never_raises_out_of_the_endpoint(client, monkeypatch):
    """A real connectivity failure (e.g. Redis unreachable) raises inside
    `get_arq_pool()`/`.ping()` - confirm the endpoint still returns 200
    with `redis.ok: false` rather than a 500, by exercising the real
    `_check_redis` against a pool getter that raises."""

    async def _raising_pool_getter():
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr(status_router, "get_arq_pool", _raising_pool_getter)
    monkeypatch.setattr(status_router, "_check_tracking", lambda: True)

    resp = client.get("/v1/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["checks"]["redis"]["ok"] is False
    assert body["status"] == "degraded"


def test_status_down_when_database_check_fails(client, monkeypatch):
    monkeypatch.setattr(status_router, "_check_database", lambda: False)
    monkeypatch.setattr(status_router, "_check_redis", _async_true)
    monkeypatch.setattr(status_router, "_check_tracking", lambda: True)

    resp = client.get("/v1/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "down"
    assert body["checks"]["database"]["ok"] is False


def test_status_never_exposes_internal_provider_names(client, monkeypatch):
    monkeypatch.setattr(status_router, "_check_redis", _async_true)
    monkeypatch.setattr(status_router, "_check_tracking", lambda: True)

    resp = client.get("/v1/status")

    text_body = resp.text.lower()
    for internal_name in ("searates", "romeu", "track_trace_browser", "oxylabs"):
        assert internal_name not in text_body


async def _async_true():
    return True
