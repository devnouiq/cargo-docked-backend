from __future__ import annotations


def test_usage_starts_with_signup_credits(client, api_key):
    resp = client.get("/v1/usage", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["credits_remaining"] == 1000
    assert body["credits_included_per_period"] == 1000


def test_tracking_a_container_deducts_one_credit(client, api_key):
    client.post("/v1/containers", json={"container_number": "MSCU1234567"}, headers={"X-API-Key": api_key})

    resp = client.get("/v1/usage", headers={"X-API-Key": api_key})
    assert resp.json()["credits_remaining"] == 999

    events = client.get("/v1/usage/events", headers={"X-API-Key": api_key})
    assert events.status_code == 200
    items = events.json()["items"]
    assert len(items) == 1
    assert items[0]["event_type"] == "container_lookup"
    assert items[0]["container_number"] == "MSCU1234567"


def test_quota_exceeded_returns_429(client, api_key, db_session):
    from app.core.security import hash_token
    from app.models.api_key import ApiKey
    from app.repositories.usage import UsageRepository

    key_row = db_session.query(ApiKey).filter_by(key_hash=hash_token(api_key)).one()
    UsageRepository().try_deduct_credits(db_session, key_row.organization_id, 1000)  # drain to zero

    resp = client.post("/v1/containers", json={"container_number": "MSCU7654321"}, headers={"X-API-Key": api_key})
    assert resp.status_code == 429
    assert resp.json()["code"] == "quota_exceeded"
