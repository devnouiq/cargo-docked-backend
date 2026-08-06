from __future__ import annotations


def test_start_tracking_creates_container(client, api_key, _fake_provider_registry):
    resp = client.post("/v1/containers", json={"container_number": "msku1234567"}, headers={"X-API-Key": api_key})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["container_number"] == "MSKU1234567"  # normalized upper
    assert body["status"] == "In Transit"
    assert body["last_known_location"] == "Rotterdam"
    assert "MSKU1234567" in _fake_provider_registry.calls


def test_get_untracked_container_is_404(client, api_key):
    resp = client.get("/v1/containers/NOPE0000000", headers={"X-API-Key": api_key})
    assert resp.status_code == 404


def test_get_tracked_container_serves_cache_without_reprovidering(client, api_key, _fake_provider_registry):
    client.post("/v1/containers", json={"container_number": "MSKU1234567"}, headers={"X-API-Key": api_key})
    assert _fake_provider_registry.calls.count("MSKU1234567") == 1

    resp = client.get("/v1/containers/MSKU1234567", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    # Still fresh (just polled) - no second live provider call.
    assert _fake_provider_registry.calls.count("MSKU1234567") == 1


def test_list_containers_returns_tracked_items(client, api_key):
    client.post("/v1/containers", json={"container_number": "MSKU1111111"}, headers={"X-API-Key": api_key})
    client.post("/v1/containers", json={"container_number": "MSKU2222222"}, headers={"X-API-Key": api_key})

    resp = client.get("/v1/containers", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {item["container_number"] for item in body["items"]} == {"MSKU1111111", "MSKU2222222"}


def test_stop_tracking_deactivates_and_removes_from_list(client, api_key):
    client.post("/v1/containers", json={"container_number": "MSKU3333333"}, headers={"X-API-Key": api_key})

    delete_resp = client.delete("/v1/containers/MSKU3333333", headers={"X-API-Key": api_key})
    assert delete_resp.status_code == 204

    listed = client.get("/v1/containers", headers={"X-API-Key": api_key}).json()
    assert listed["total"] == 0

    delete_again = client.delete("/v1/containers/MSKU3333333", headers={"X-API-Key": api_key})
    assert delete_again.status_code == 404


def test_bulk_tracking_registers_even_when_provider_cannot_resolve_yet(client, api_key):
    """POST /v1/containers registers tracking intent - a number no provider
    can currently resolve (e.g. not yet in the carrier's system) still
    succeeds as "now tracked, no data yet", not an error. The poller
    retries it later. Only a genuine exception should isolate per-item."""
    resp = client.post(
        "/v1/containers/bulk",
        json={"container_numbers": ["MSKU4444444", "MISS0000001"]},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    results = {r["container_number"]: r for r in resp.json()["results"]}
    assert results["MSKU4444444"]["ok"] is True
    assert results["MSKU4444444"]["container"]["status"] == "In Transit"

    assert results["MISS0000001"]["ok"] is True
    assert results["MISS0000001"]["container"]["status"] is None


def test_deprecated_track_alias_still_works(client, api_key):
    resp = client.get("/v1/track/MSKU5555555", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.json()["container_number"] == "MSKU5555555"


def test_containers_route_requires_api_key_not_jwt(client, signed_up_org):
    """/v1/containers is API-key authenticated - a dashboard session (JWT)
    alone must not work, mirroring the JWT-vs-API-key split documented in
    routers/v1/__init__.py."""
    _tokens, jwt_headers = signed_up_org
    resp = client.get("/v1/containers", headers=jwt_headers)
    assert resp.status_code == 401
