"""Billing routes - JWT-authenticated. Stripe isn't configured in tests
(no STRIPE_SECRET_KEY), so the point here is confirming the app degrades
to a clean 503 instead of crashing, and that the plan/subscription reads
that don't need Stripe still work.
"""

from __future__ import annotations

from app.services.billing_service import seed_default_plans


def test_list_plans_returns_seeded_defaults(client, db_session):
    seed_default_plans(db_session)
    resp = client.get("/v1/billing/plans")
    assert resp.status_code == 200
    codes = {p["code"] for p in resp.json()}
    assert codes == {"free", "starter", "growth", "enterprise"}


def test_subscription_404_when_org_has_none(client, signed_up_org):
    _tokens, headers = signed_up_org
    resp = client.get("/v1/billing/subscription", headers=headers)
    assert resp.status_code == 404


def test_checkout_session_without_stripe_configured_returns_503(client, signed_up_org, db_session):
    seed_default_plans(db_session)
    _tokens, headers = signed_up_org
    resp = client.post(
        "/v1/billing/checkout-session",
        json={"plan_code": "starter", "success_url": "https://example.com/ok", "cancel_url": "https://example.com/cancel"},
        headers=headers,
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "feature_not_configured"


def test_portal_session_without_stripe_configured_returns_503(client, signed_up_org):
    _tokens, headers = signed_up_org
    resp = client.post("/v1/billing/portal-session", json={"return_url": "https://example.com/account"}, headers=headers)
    assert resp.status_code == 503


def test_billing_routes_require_a_session(client):
    resp = client.get("/v1/billing/subscription")
    assert resp.status_code == 401
