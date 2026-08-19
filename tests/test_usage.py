from __future__ import annotations


def test_usage_starts_with_signup_credits(client, api_key):
    resp = client.get("/v1/usage", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["credits_remaining"] == 10
    assert body["credits_included_per_period"] == 10


def test_tracking_a_container_deducts_one_credit(client, api_key):
    client.post("/v1/containers", json={"container_number": "MSCU1234567"}, headers={"X-API-Key": api_key})

    resp = client.get("/v1/usage", headers={"X-API-Key": api_key})
    assert resp.json()["credits_remaining"] == 9

    events = client.get("/v1/usage/events", headers={"X-API-Key": api_key})
    assert events.status_code == 200
    items = events.json()["items"]
    assert len(items) == 1
    assert items[0]["event_type"] == "container_lookup"
    assert items[0]["container_number"] == "MSCU1234567"


def test_free_plan_exhausted_returns_429_upgrade_message(client, api_key, db_session):
    """A signed-up org with no Subscription row is still on the one-time
    free-signup grant, which never renews - the 429 it gets back once that
    runs out must say so (not "wait for the next billing period")."""
    from app.core.security import hash_token
    from app.models.api_key import ApiKey
    from app.repositories.usage import UsageRepository

    key_row = db_session.query(ApiKey).filter_by(key_hash=hash_token(api_key)).one()
    UsageRepository().try_deduct_credits(db_session, key_row.organization_id, 10)  # drain to zero

    resp = client.post("/v1/containers", json={"container_number": "MSCU7654321"}, headers={"X-API-Key": api_key})
    assert resp.status_code == 429
    body = resp.json()
    assert body["code"] == "free_credits_exhausted"
    assert "upgrade" in body["detail"].lower()


def test_lapsed_subscription_returns_plan_expired(client, api_key, db_session):
    """An org whose paid subscription lapsed (canceled/past_due) but still
    has a Subscription row must get a "plan expired" 429, not the generic
    mid-cycle quota-exceeded message."""
    from app.core.security import hash_token
    from app.models.api_key import ApiKey
    from app.models.billing import Plan, Subscription, SubscriptionStatus
    from app.repositories.usage import UsageRepository

    key_row = db_session.query(ApiKey).filter_by(key_hash=hash_token(api_key)).one()
    org_id = key_row.organization_id
    plan = db_session.query(Plan).filter_by(code="feeder").one()
    db_session.add(Subscription(organization_id=org_id, plan_id=plan.id, status=SubscriptionStatus.CANCELED))
    db_session.commit()

    UsageRepository().try_deduct_credits(db_session, org_id, 10)  # drain the leftover free-signup credits

    resp = client.post("/v1/containers", json={"container_number": "MSCU7654321"}, headers={"X-API-Key": api_key})
    assert resp.status_code == 429
    body = resp.json()
    assert body["code"] == "plan_expired"
    assert "expired" in body["detail"].lower()
