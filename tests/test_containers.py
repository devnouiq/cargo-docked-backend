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


def test_list_containers_orders_by_most_recently_scraped_first(client, api_key, fake_arq_pool):
    """Re-scraping an older container should bump it back to the top of the
    list, not leave it wherever it was originally created."""
    client.post("/v1/containers", json={"container_number": "MSKU1111111"}, headers={"X-API-Key": api_key})
    client.post("/v1/containers", json={"container_number": "MSKU2222222"}, headers={"X-API-Key": api_key})

    refreshed = client.post("/v1/containers/MSKU1111111/refresh", headers={"X-API-Key": api_key})
    assert refreshed.status_code == 202, refreshed.text

    listed = client.get("/v1/containers", headers={"X-API-Key": api_key}).json()
    assert [item["container_number"] for item in listed["items"]] == ["MSKU1111111", "MSKU2222222"]


def test_stop_tracking_deactivates_and_removes_from_list(client, api_key):
    client.post("/v1/containers", json={"container_number": "MSKU3333333"}, headers={"X-API-Key": api_key})

    delete_resp = client.delete("/v1/containers/MSKU3333333", headers={"X-API-Key": api_key})
    assert delete_resp.status_code == 204

    listed = client.get("/v1/containers", headers={"X-API-Key": api_key}).json()
    assert listed["total"] == 0

    delete_again = client.delete("/v1/containers/MSKU3333333", headers={"X-API-Key": api_key})
    assert delete_again.status_code == 404


def test_bulk_tracking_queues_every_number_without_scraping_inline(client, api_key, _fake_provider_registry):
    """Bulk registers tracking intent and hands the scraping to the worker.

    Every accepted number - including one no provider will ever resolve -
    comes back `ok: true` / `queued` with no carrier data yet; resolvability
    is the worker's problem, and "not in the carrier's system yet" was never
    an error to begin with (the poller retries it).
    """
    resp = client.post(
        "/v1/containers/bulk",
        json={"container_numbers": ["MSKU4444444", "MISS0000001"]},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert (body["queued"], body["rejected"]) == (2, 0)

    results = {r["container_number"]: r for r in body["results"]}
    for number in ("MSKU4444444", "MISS0000001"):
        assert results[number]["ok"] is True
        assert results[number]["container"]["scrape_status"] == "queued"
        assert results[number]["container"]["status"] is None  # nothing scraped yet

    # The whole point of the change: the request no longer blocks on a
    # provider call, so none happened.
    assert _fake_provider_registry.calls == []


def test_bulk_enqueues_one_scrape_job_per_container(client, api_key, fake_arq_pool):
    resp = client.post(
        "/v1/containers/bulk",
        json={"container_numbers": ["MSKU4444444", "MSKU5555555"]},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 202, resp.text

    ids_by_number = {r["container_number"]: r["container"]["id"] for r in resp.json()["results"]}
    assert [name for name, _args, _kwargs in fake_arq_pool.enqueued] == ["scrape_container", "scrape_container"]
    enqueued_ids = {args[0] for _name, args, _kwargs in fake_arq_pool.enqueued}
    assert enqueued_ids == set(ids_by_number.values())


def test_bulk_deduplicates_repeated_numbers_in_one_payload(client, api_key, fake_arq_pool):
    """Same number twice in one payload: one row, one credit, one job."""
    before = client.get("/v1/usage", headers={"X-API-Key": api_key}).json()["credits_remaining"]

    resp = client.post(
        "/v1/containers/bulk",
        json={"container_numbers": ["MSKU4444444", "msku4444444", " MSKU4444444 "]},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["queued"] == 1
    assert body["results"][0]["container_number"] == "MSKU4444444"

    assert len(fake_arq_pool.enqueued) == 1
    assert client.get("/v1/containers", headers={"X-API-Key": api_key}).json()["total"] == 1

    after = client.get("/v1/usage", headers={"X-API-Key": api_key}).json()["credits_remaining"]
    assert after == before - 1


def test_bulk_charges_exactly_one_credit_per_unique_container(client, api_key):
    """Regression guard against double-charging: submission charges once per
    container, and the worker's later scrape charges nothing on top."""
    before = client.get("/v1/usage", headers={"X-API-Key": api_key}).json()["credits_remaining"]

    resp = client.post(
        "/v1/containers/bulk",
        json={"container_numbers": ["MSKU1111111", "MSKU2222222", "MSKU3333333"]},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 202, resp.text

    after = client.get("/v1/usage", headers={"X-API-Key": api_key}).json()["credits_remaining"]
    assert after == before - 3


def test_bulk_survives_the_arq_pool_being_unreachable(client, api_key, monkeypatch):
    """Redis outage: rows are already committed, so the client still gets a
    202 and the rows sit `queued` for the poller to sweep."""
    import app.services.container_service as container_service_module

    async def _exploding_pool():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(container_service_module, "get_arq_pool", _exploding_pool)

    resp = client.post(
        "/v1/containers/bulk", json={"container_numbers": ["MSKU4444444"]}, headers={"X-API-Key": api_key}
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["queued"] == 1

    listed = client.get("/v1/containers", headers={"X-API-Key": api_key}).json()
    assert listed["total"] == 1
    assert listed["items"][0]["scrape_status"] == "queued"


def test_bulk_rejects_items_it_cannot_charge_for(client, api_key, db_session, fake_arq_pool):
    """An org out of credits gets per-item rejections, not free rows and not
    a whole-batch 429 - the batch is still 202, item-level `ok: false`."""
    from app.core.security import hash_token
    from app.models.api_key import ApiKey
    from app.repositories.usage import UsageRepository

    key_row = db_session.query(ApiKey).filter_by(key_hash=hash_token(api_key)).one()
    UsageRepository().try_deduct_credits(db_session, key_row.organization_id, 1000)  # drain to zero
    db_session.commit()

    resp = client.post(
        "/v1/containers/bulk", json={"container_numbers": ["MSKU4444444"]}, headers={"X-API-Key": api_key}
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert (body["queued"], body["rejected"]) == (0, 1)
    assert body["results"][0]["ok"] is False
    assert body["results"][0]["container"] is None
    assert body["results"][0]["error"]

    assert fake_arq_pool.enqueued == []
    assert client.get("/v1/containers", headers={"X-API-Key": api_key}).json()["total"] == 0


def test_refresh_endpoint_charges_and_enqueues_once(client, api_key, fake_arq_pool):
    created = client.post(
        "/v1/containers", json={"container_number": "MSKU7777777"}, headers={"X-API-Key": api_key}
    )
    assert created.status_code == 201, created.text
    assert created.json()["scrape_status"] == "succeeded"  # sync POST scraped inline
    fake_arq_pool.enqueued.clear()

    before = client.get("/v1/usage", headers={"X-API-Key": api_key}).json()["credits_remaining"]
    resp = client.post("/v1/containers/MSKU7777777/refresh", headers={"X-API-Key": api_key})
    assert resp.status_code == 202, resp.text
    assert resp.json()["scrape_status"] == "queued"

    assert len(fake_arq_pool.enqueued) == 1
    name, args, _kwargs = fake_arq_pool.enqueued[0]
    assert name == "scrape_container"
    assert args == (created.json()["id"],)
    assert client.get("/v1/usage", headers={"X-API-Key": api_key}).json()["credits_remaining"] == before - 1


def test_refresh_is_idempotent_while_a_scrape_is_still_pending(client, api_key, fake_arq_pool):
    """A double-clicked refresh button must not bill twice."""
    client.post("/v1/containers", json={"container_number": "MSKU7777777"}, headers={"X-API-Key": api_key})
    client.post("/v1/containers/MSKU7777777/refresh", headers={"X-API-Key": api_key})
    fake_arq_pool.enqueued.clear()

    before = client.get("/v1/usage", headers={"X-API-Key": api_key}).json()["credits_remaining"]
    again = client.post("/v1/containers/MSKU7777777/refresh", headers={"X-API-Key": api_key})

    assert again.status_code == 202, again.text
    assert again.json()["scrape_status"] == "queued"
    assert fake_arq_pool.enqueued == []
    assert client.get("/v1/usage", headers={"X-API-Key": api_key}).json()["credits_remaining"] == before


def test_refresh_unknown_container_is_404(client, api_key, fake_arq_pool):
    resp = client.post("/v1/containers/NOPE0000000/refresh", headers={"X-API-Key": api_key})
    assert resp.status_code == 404
    assert fake_arq_pool.enqueued == []


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
