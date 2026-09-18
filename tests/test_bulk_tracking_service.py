"""Regression coverage for the bulk-preview crash found via a live
2,000-container stress test against production: any single container
whose attempt failed (timeout, rate limit, or GoComet's auto-suggest not
recognizing its carrier) produced a result dict with no `found` key.
`TrackingBulkItemOut.found` is a plain `bool`, not `bool | None`, so
`searates_debug.track_searates_bulk`'s
`TrackingBulkItemOut(**{field: (r or {}).get(field) ...})` raised a
Pydantic ValidationError building *that one item* - uncaught, which
crashed the whole batch's response and discarded every other container's
already-resolved result along with it. Reproduced live: a batch of exactly
one unresolvable-carrier container 500'd.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import bulk_tracking_service as svc
import app.routers.searates_debug as searates_debug


class _FakePool:
    """Minimal stand-in for GoCometSessionPool - _attempt is monkeypatched
    below so the tracker object it hands out is never actually used."""

    def acquire(self):
        return object()

    def discard(self, tracker):
        pass

    def release(self, tracker):
        pass

    def pending_spares(self):
        return 0


@pytest.mark.asyncio
async def test_worker_failure_result_always_has_found_false(monkeypatch):
    async def _raise(tracker, number, sealine):
        raise RuntimeError("simulated GoComet failure")

    monkeypatch.setattr(svc, "_attempt", _raise)

    queue: "asyncio.Queue[tuple[int, str]]" = asyncio.Queue()
    queue.put_nowait((0, "BADU1234567"))
    results: list[dict | None] = [None]

    await svc._worker(0, queue, results, "AUTO", _FakePool())

    assert results[0] is not None
    assert "found" in results[0]
    assert results[0]["found"] is False
    assert results[0]["invalid_reason"] == "simulated GoComet failure"


@pytest.mark.asyncio
async def test_track_many_parallel_mixed_success_and_failure_never_crashes(monkeypatch):
    """One container fails, one succeeds, in the same batch - the batch as
    a whole must still complete (this is what the router then turns into
    TrackingBulkItemOut for each item)."""

    async def _attempt(tracker, number, sealine):
        if number == "GOODU0000001":
            return tracker, {"number": number, "found": True, "status": "in_transit"}
        raise RuntimeError("carrier not resolved")

    monkeypatch.setattr(svc, "_attempt", _attempt)
    monkeypatch.setattr(svc, "get_pool", lambda kwargs: _FakePool())

    result = await svc.track_many_parallel(["GOODU0000001", "BADU1234567"], batch_size=2)

    by_number = {r["number"]: r for r in result["results"]}
    assert by_number["GOODU0000001"]["found"] is True
    assert by_number["BADU1234567"]["found"] is False


def test_bulk_endpoint_does_not_500_when_one_item_fails(client, monkeypatch):
    """HTTP-level regression test for the exact live repro: POST a batch
    where one item's attempt fails - must come back 200 with both items
    represented, never a bare 500 that discards the whole batch."""

    async def _fake_track_many_parallel(numbers, sealine="AUTO", batch_size=10, **kwargs):
        return {
            "total": len(numbers),
            "batch_size": batch_size,
            "total_duration_seconds": 0.1,
            "success_count": 1,
            "error_count": 1,
            "throughput_per_sec": 20.0,
            "results": [
                {"number": "GOODU0000001", "found": True, "status": "in_transit", "display_status": "In Transit"},
                {"number": "BADU1234567", "found": False, "status": "error", "invalid_reason": "carrier not resolved"},
            ],
        }

    monkeypatch.setattr(searates_debug, "track_many_parallel", _fake_track_many_parallel)

    resp = client.post(
        "/v1/track-searates/bulk",
        json={"container_numbers": ["GOODU0000001", "BADU1234567"], "batch_size": 2, "sealine": "AUTO"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    by_number = {r["number"]: r for r in body["results"]}
    assert by_number["GOODU0000001"]["found"] is True
    assert by_number["BADU1234567"]["found"] is False
