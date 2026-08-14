"""Billing routes - JWT-authenticated. Stripe isn't configured in tests
(no STRIPE_SECRET_KEY), so the point here is confirming the app degrades
to a clean 503 instead of crashing, and that the plan/subscription reads
that don't need Stripe still work.
"""

from __future__ import annotations

from app.core.security import hash_token
from app.models.api_key import ApiKey
from app.models.billing import Plan
from app.models.usage import UsageEventType
from app.services.billing_service import BillingService, seed_default_plans
from app.services.usage_service import UsageService


def _org_id_for(db_session, api_key: str):
    return db_session.query(ApiKey).filter_by(key_hash=hash_token(api_key)).one().organization_id


def _stripe_subscription(*, price_id: str, subscription_id: str = "sub_123") -> dict:
    return {
        "id": subscription_id,
        "customer": "cus_123",
        "status": "active",
        "items": {"data": [{"price": {"id": price_id}}]},
    }


def _stripe_invoice(*, subscription_id: str, billing_reason: str = "subscription_cycle") -> dict:
    # This SDK's pinned API version nests the generating subscription under
    # parent.subscription_details.subscription rather than a top-level
    # `subscription` field - see billing_service.refill_credits_for_invoice.
    return {
        "id": "in_123",
        "billing_reason": billing_reason,
        "parent": {"type": "subscription_details", "subscription_details": {"subscription": subscription_id}},
    }


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


def test_invoices_without_stripe_configured_returns_503(client, signed_up_org):
    _tokens, headers = signed_up_org
    resp = client.get("/v1/billing/invoices", headers=headers)
    assert resp.status_code == 503
    assert resp.json()["code"] == "feature_not_configured"


def test_payment_method_without_stripe_configured_returns_503(client, signed_up_org):
    _tokens, headers = signed_up_org
    resp = client.get("/v1/billing/payment-method", headers=headers)
    assert resp.status_code == 503


def test_billing_routes_require_a_session(client):
    resp = client.get("/v1/billing/subscription")
    assert resp.status_code == 401


def test_plan_change_refills_credits_to_the_new_plans_allotment(client, db_session, api_key):
    org_id = _org_id_for(db_session, api_key)
    usage = UsageService()

    # Spend some of the Free plan's starting 1,000 credits first, so a
    # refill is actually observable rather than trivially still the default.
    usage.charge(
        db_session, organization_id=org_id, api_key_id=None,
        event_type=UsageEventType.CONTAINER_LOOKUP, credits=133,
    )
    assert usage.get_balance(db_session, org_id).credits_remaining == 867

    starter_price_id = "price_starter_test"
    db_session.query(Plan).filter_by(code="starter").update({"stripe_price_id": starter_price_id})
    db_session.commit()

    BillingService().upsert_subscription_from_stripe_object(
        db_session, organization_id=org_id, stripe_subscription=_stripe_subscription(price_id=starter_price_id),
    )

    balance = usage.get_balance(db_session, org_id)
    assert balance.credits_remaining == 10_000
    assert balance.credits_included_per_period == 10_000


def test_repeated_same_plan_webhook_does_not_re_refill_credits(client, db_session, api_key):
    """Stripe delivers webhooks at-least-once - a retried/duplicate
    `subscription.updated` for a plan the org is already on (or one fired
    for an unrelated change, e.g. a payment method update) must not wipe
    out credits already spent this period."""
    org_id = _org_id_for(db_session, api_key)
    usage = UsageService()

    starter_price_id = "price_starter_test"
    db_session.query(Plan).filter_by(code="starter").update({"stripe_price_id": starter_price_id})
    db_session.commit()

    BillingService().upsert_subscription_from_stripe_object(
        db_session, organization_id=org_id, stripe_subscription=_stripe_subscription(price_id=starter_price_id),
    )
    usage.charge(
        db_session, organization_id=org_id, api_key_id=None,
        event_type=UsageEventType.CONTAINER_LOOKUP, credits=500,
    )
    assert usage.get_balance(db_session, org_id).credits_remaining == 9_500

    # Same plan, new webhook delivery (e.g. Stripe retry, or an unrelated
    # subscription.updated) - must not reset the balance back to 10,000.
    BillingService().upsert_subscription_from_stripe_object(
        db_session,
        organization_id=org_id,
        stripe_subscription=_stripe_subscription(price_id=starter_price_id, subscription_id="sub_123"),
    )

    assert usage.get_balance(db_session, org_id).credits_remaining == 9_500


def test_invoice_paid_refills_credits_on_renewal(client, db_session, api_key):
    """Covers both monthly and annual renewals identically - Stripe fires
    `invoice.paid` the same way regardless of the plan's billing interval,
    and refill_credits_for_invoice doesn't look at the interval at all, so
    a `subscription_cycle` invoice resets the balance to the plan's
    allotment either way."""
    org_id = _org_id_for(db_session, api_key)
    usage = UsageService()

    starter_price_id = "price_starter_test"
    db_session.query(Plan).filter_by(code="starter").update({"stripe_price_id": starter_price_id})
    db_session.commit()

    BillingService().upsert_subscription_from_stripe_object(
        db_session, organization_id=org_id, stripe_subscription=_stripe_subscription(price_id=starter_price_id),
    )
    usage.charge(
        db_session, organization_id=org_id, api_key_id=None,
        event_type=UsageEventType.CONTAINER_LOOKUP, credits=9_500,
    )
    assert usage.get_balance(db_session, org_id).credits_remaining == 500

    # A new billing period starts (renewal invoice paid) - balance resets
    # to the plan's full allotment, not an additive top-up.
    BillingService().refill_credits_for_invoice(db_session, invoice=_stripe_invoice(subscription_id="sub_123"))

    balance = usage.get_balance(db_session, org_id)
    assert balance.credits_remaining == 10_000
    assert balance.credits_included_per_period == 10_000


def test_invoice_paid_for_unknown_subscription_is_a_noop(db_session, api_key):
    """Webhook delivery order isn't guaranteed - an invoice.paid can arrive
    before the subscription.created event that creates our local row.
    Must log and skip, not crash the webhook handler."""
    org_id = _org_id_for(db_session, api_key)
    usage = UsageService()
    before = usage.get_balance(db_session, org_id).credits_remaining

    BillingService().refill_credits_for_invoice(db_session, invoice=_stripe_invoice(subscription_id="sub_never_seen"))

    assert usage.get_balance(db_session, org_id).credits_remaining == before


def test_invoice_paid_for_a_one_off_invoice_is_ignored(db_session, api_key):
    """An invoice with no generating subscription (e.g. a manual one-off
    invoice) has no `parent.subscription_details` at all - must be a noop,
    not a KeyError."""
    org_id = _org_id_for(db_session, api_key)
    usage = UsageService()
    before = usage.get_balance(db_session, org_id).credits_remaining

    BillingService().refill_credits_for_invoice(db_session, invoice={"id": "in_manual", "parent": None})

    assert usage.get_balance(db_session, org_id).credits_remaining == before
