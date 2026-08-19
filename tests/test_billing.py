"""Billing routes - JWT-authenticated. Stripe isn't configured in tests
(no STRIPE_SECRET_KEY), so the point here is confirming the app degrades
to a clean 503 instead of crashing, and that the plan/subscription reads
that don't need Stripe still work.
"""

from __future__ import annotations

import types

import stripe

from app.core.config import settings
from app.core.security import hash_token
from app.models.api_key import ApiKey
from app.models.billing import Plan
from app.models.usage import UsageEventType
from app.services.billing_service import BillingService, seed_default_plans
from app.services.usage_service import UsageService


def _org_id_for(db_session, api_key: str):
    return db_session.query(ApiKey).filter_by(key_hash=hash_token(api_key)).one().organization_id


def _configure_stripe(monkeypatch):
    """Point the shared `settings` singleton at a fake Stripe key so
    `_require_stripe()` doesn't 503 in tests that need to reach the (mocked)
    Stripe SDK calls. `settings` is the one object every module imports
    (`from ..core.config import settings`), so mutating it here affects
    billing_service.py too; monkeypatch reverts it after the test."""
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_fake")


def _fake_stripe_session(url: str = "https://checkout.stripe.example.com/cs_test_1", session_id: str = "cs_test_1"):
    # Real stripe.checkout.Session objects support attribute access
    # (session.url) - a SimpleNamespace is a lightweight stand-in.
    return types.SimpleNamespace(url=url, id=session_id)


class _FakeStripeObject(dict):
    """Stand-in for a real stripe.StripeObject (event['data']['object']) -
    supports .to_dict() the way the webhook handler requires (see
    CLAUDE.md's note on this SDK's .get() quirk)."""

    def to_dict(self):
        return dict(self)


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
    assert codes == {"free", "feeder", "panamax", "ultra", "fleet", "enterprise"}


def test_subscription_404_when_org_has_none(client, signed_up_org):
    _tokens, headers = signed_up_org
    resp = client.get("/v1/billing/subscription", headers=headers)
    assert resp.status_code == 404


def test_checkout_session_without_stripe_configured_returns_503(client, signed_up_org, db_session):
    seed_default_plans(db_session)
    _tokens, headers = signed_up_org
    resp = client.post(
        "/v1/billing/checkout-session",
        json={"plan_code": "feeder", "success_url": "https://example.com/ok", "cancel_url": "https://example.com/cancel"},
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

    # Spend some of the Free plan's starting 10 credits first, so a
    # refill is actually observable rather than trivially still the default.
    usage.charge(
        db_session, organization_id=org_id, api_key_id=None,
        event_type=UsageEventType.CONTAINER_LOOKUP, credits=3,
    )
    assert usage.get_balance(db_session, org_id).credits_remaining == 7

    starter_price_id = "price_starter_test"
    db_session.query(Plan).filter_by(code="feeder").update({"stripe_price_id": starter_price_id})
    db_session.commit()

    BillingService().upsert_subscription_from_stripe_object(
        db_session, organization_id=org_id, stripe_subscription=_stripe_subscription(price_id=starter_price_id),
    )

    balance = usage.get_balance(db_session, org_id)
    assert balance.credits_remaining == 1_500
    assert balance.credits_included_per_period == 1_500


def test_repeated_same_plan_webhook_does_not_re_refill_credits(client, db_session, api_key):
    """Stripe delivers webhooks at-least-once - a retried/duplicate
    `subscription.updated` for a plan the org is already on (or one fired
    for an unrelated change, e.g. a payment method update) must not wipe
    out credits already spent this period."""
    org_id = _org_id_for(db_session, api_key)
    usage = UsageService()

    starter_price_id = "price_starter_test"
    db_session.query(Plan).filter_by(code="feeder").update({"stripe_price_id": starter_price_id})
    db_session.commit()

    BillingService().upsert_subscription_from_stripe_object(
        db_session, organization_id=org_id, stripe_subscription=_stripe_subscription(price_id=starter_price_id),
    )
    usage.charge(
        db_session, organization_id=org_id, api_key_id=None,
        event_type=UsageEventType.CONTAINER_LOOKUP, credits=500,
    )
    assert usage.get_balance(db_session, org_id).credits_remaining == 1_000

    # Same plan, new webhook delivery (e.g. Stripe retry, or an unrelated
    # subscription.updated) - must not reset the balance back to 1,500.
    BillingService().upsert_subscription_from_stripe_object(
        db_session,
        organization_id=org_id,
        stripe_subscription=_stripe_subscription(price_id=starter_price_id, subscription_id="sub_123"),
    )

    assert usage.get_balance(db_session, org_id).credits_remaining == 1_000


def test_invoice_paid_refills_credits_on_renewal(client, db_session, api_key):
    """Covers both monthly and annual renewals identically - Stripe fires
    `invoice.paid` the same way regardless of the plan's billing interval,
    and refill_credits_for_invoice doesn't look at the interval at all, so
    a `subscription_cycle` invoice resets the balance to the plan's
    allotment either way."""
    org_id = _org_id_for(db_session, api_key)
    usage = UsageService()

    starter_price_id = "price_starter_test"
    db_session.query(Plan).filter_by(code="feeder").update({"stripe_price_id": starter_price_id})
    db_session.commit()

    BillingService().upsert_subscription_from_stripe_object(
        db_session, organization_id=org_id, stripe_subscription=_stripe_subscription(price_id=starter_price_id),
    )
    usage.charge(
        db_session, organization_id=org_id, api_key_id=None,
        event_type=UsageEventType.CONTAINER_LOOKUP, credits=1_000,
    )
    assert usage.get_balance(db_session, org_id).credits_remaining == 500

    # A new billing period starts (renewal invoice paid) - balance resets
    # to the plan's full allotment, not an additive top-up.
    BillingService().refill_credits_for_invoice(db_session, invoice=_stripe_invoice(subscription_id="sub_123"))

    balance = usage.get_balance(db_session, org_id)
    assert balance.credits_remaining == 1_500
    assert balance.credits_included_per_period == 1_500


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


# ── USD/EUR currency support ─────────────────────────────────────────────


def test_list_plans_returns_both_eur_and_usd_price_fields(client, db_session):
    seed_default_plans(db_session)
    resp = client.get("/v1/billing/plans")
    assert resp.status_code == 200
    by_code = {p["code"]: p for p in resp.json()}

    assert by_code["feeder"]["monthly_price_cents"] == 5_000
    assert by_code["feeder"]["monthly_price_cents_usd"] == 5_500
    assert by_code["panamax"]["monthly_price_cents"] == 12_000
    assert by_code["panamax"]["monthly_price_cents_usd"] == 13_000
    # Free/Enterprise never get a Stripe price in either currency.
    assert by_code["free"]["monthly_price_cents_usd"] is None
    assert by_code["enterprise"]["monthly_price_cents_usd"] is None


def test_checkout_session_currency_usd_selects_the_usd_stripe_price_id(client, signed_up_org, db_session, monkeypatch):
    _tokens, headers = signed_up_org
    seed_default_plans(db_session)
    db_session.query(Plan).filter_by(code="feeder").update(
        {"stripe_price_id": "price_eur_test", "stripe_price_id_usd": "price_usd_test"}
    )
    db_session.commit()
    _configure_stripe(monkeypatch)

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_stripe_session()

    monkeypatch.setattr(stripe.checkout.Session, "create", fake_create)

    resp = client.post(
        "/v1/billing/checkout-session",
        json={
            "plan_code": "feeder",
            "success_url": "https://example.com/ok",
            "cancel_url": "https://example.com/cancel",
            "currency": "usd",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["checkout_url"] == _fake_stripe_session().url
    assert captured["line_items"] == [{"price": "price_usd_test", "quantity": 1}]
    # No vat_number was sent, so automatic_tax (which requires Stripe Tax
    # configured on the account) is correctly omitted - see
    # create_checkout_session's docstring/comment for why it's gated on
    # vat_number rather than always on.
    assert "automatic_tax" not in captured
    assert captured["tax_id_collection"] == {"enabled": True}


def test_checkout_session_currency_eur_still_selects_the_eur_stripe_price_id(client, signed_up_org, db_session, monkeypatch):
    """Default currency stays eur - a USD price being configured must not
    change what an unqualified (or explicit currency=eur) request gets."""
    _tokens, headers = signed_up_org
    seed_default_plans(db_session)
    db_session.query(Plan).filter_by(code="feeder").update(
        {"stripe_price_id": "price_eur_test", "stripe_price_id_usd": "price_usd_test"}
    )
    db_session.commit()
    _configure_stripe(monkeypatch)

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_stripe_session()

    monkeypatch.setattr(stripe.checkout.Session, "create", fake_create)

    resp = client.post(
        "/v1/billing/checkout-session",
        json={"plan_code": "feeder", "success_url": "https://example.com/ok", "cancel_url": "https://example.com/cancel"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert captured["line_items"] == [{"price": "price_eur_test", "quantity": 1}]


def test_checkout_session_currency_usd_without_a_usd_price_configured_returns_503(client, signed_up_org, db_session, monkeypatch):
    _tokens, headers = signed_up_org
    # Force the USD price unset regardless of what's in the real .env this
    # test run happens to have (a local dev environment may have real Stripe
    # price ids configured for live testing - see _configure_stripe above).
    monkeypatch.setattr(settings, "stripe_feeder_price_id_usd", None)
    seed_default_plans(db_session)
    _configure_stripe(monkeypatch)

    resp = client.post(
        "/v1/billing/checkout-session",
        json={
            "plan_code": "feeder",
            "success_url": "https://example.com/ok",
            "cancel_url": "https://example.com/cancel",
            "currency": "usd",
        },
        headers=headers,
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "feature_not_configured"


# ── VAT number / EU reverse-charge ───────────────────────────────────────


def test_checkout_session_with_vat_number_creates_a_customer_and_attaches_the_tax_id(
    client, signed_up_org, db_session, monkeypatch
):
    _tokens, headers = signed_up_org
    seed_default_plans(db_session)
    db_session.query(Plan).filter_by(code="feeder").update({"stripe_price_id": "price_eur_test"})
    db_session.commit()
    _configure_stripe(monkeypatch)

    created_customers = []
    tax_ids = []
    session_kwargs = {}

    def fake_customer_create(**kwargs):
        created_customers.append(kwargs)
        return types.SimpleNamespace(id="cus_new_123")

    def fake_create_tax_id(customer_id, **kwargs):
        tax_ids.append((customer_id, kwargs))
        return types.SimpleNamespace(id="txi_123")

    def fake_session_create(**kwargs):
        session_kwargs.update(kwargs)
        return _fake_stripe_session()

    monkeypatch.setattr(stripe.Customer, "create", fake_customer_create)
    monkeypatch.setattr(stripe.Customer, "create_tax_id", fake_create_tax_id)
    monkeypatch.setattr(stripe.checkout.Session, "create", fake_session_create)

    resp = client.post(
        "/v1/billing/checkout-session",
        json={
            "plan_code": "feeder",
            "success_url": "https://example.com/ok",
            "cancel_url": "https://example.com/cancel",
            "vat_number": "DE123456789",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    # No existing subscription/customer for this brand-new org - a Customer
    # must be created explicitly (up front) so the tax ID has somewhere to
    # attach, rather than letting Checkout create one implicitly.
    assert len(created_customers) == 1
    assert created_customers[0]["email"] == "founder@example.com"
    assert tax_ids == [("cus_new_123", {"type": "eu_vat", "value": "DE123456789"})]
    assert session_kwargs["customer"] == "cus_new_123"


def test_checkout_session_with_a_gb_vat_number_uses_the_gb_vat_tax_type(client, signed_up_org, db_session, monkeypatch):
    _tokens, headers = signed_up_org
    seed_default_plans(db_session)
    db_session.query(Plan).filter_by(code="feeder").update({"stripe_price_id": "price_eur_test"})
    db_session.commit()
    _configure_stripe(monkeypatch)

    tax_ids = []
    monkeypatch.setattr(stripe.Customer, "create", lambda **kw: types.SimpleNamespace(id="cus_gb_1"))
    monkeypatch.setattr(
        stripe.Customer, "create_tax_id", lambda customer_id, **kw: tax_ids.append((customer_id, kw))
    )
    monkeypatch.setattr(stripe.checkout.Session, "create", lambda **kw: _fake_stripe_session())

    resp = client.post(
        "/v1/billing/checkout-session",
        json={
            "plan_code": "feeder",
            "success_url": "https://example.com/ok",
            "cancel_url": "https://example.com/cancel",
            "vat_number": "GB999999973",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert tax_ids == [("cus_gb_1", {"type": "gb_vat", "value": "GB999999973"})]


def test_checkout_session_vat_id_attachment_failure_surfaces_as_upstream_provider_error(
    client, signed_up_org, db_session, monkeypatch
):
    """If the connected Stripe account doesn't have Stripe Tax enabled (or
    any other Stripe-side rejection of the tax id), the failure must come
    back as a typed 502, not a raw 500 - same error-wrapping pattern as
    every other Stripe call in this service."""
    _tokens, headers = signed_up_org
    seed_default_plans(db_session)
    db_session.query(Plan).filter_by(code="feeder").update({"stripe_price_id": "price_eur_test"})
    db_session.commit()
    _configure_stripe(monkeypatch)

    monkeypatch.setattr(stripe.Customer, "create", lambda **kw: types.SimpleNamespace(id="cus_err_1"))

    def fake_create_tax_id(customer_id, **kw):
        raise stripe.InvalidRequestError("Tax IDs are not supported for this account", param=None)

    monkeypatch.setattr(stripe.Customer, "create_tax_id", fake_create_tax_id)

    resp = client.post(
        "/v1/billing/checkout-session",
        json={
            "plan_code": "feeder",
            "success_url": "https://example.com/ok",
            "cancel_url": "https://example.com/cancel",
            "vat_number": "DE123456789",
        },
        headers=headers,
    )
    assert resp.status_code == 502
    assert resp.json()["code"] == "upstream_provider_error"


# ── Pay-per-credit (one-off top-up) checkout ─────────────────────────────


def test_credit_checkout_session_creates_a_payment_mode_session_with_inline_pricing(client, signed_up_org, monkeypatch):
    _tokens, headers = signed_up_org
    _configure_stripe(monkeypatch)

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_stripe_session(url="https://checkout.stripe.example.com/cs_credits")

    monkeypatch.setattr(stripe.checkout.Session, "create", fake_create)

    resp = client.post(
        "/v1/billing/credits/checkout-session",
        json={
            "credits": 2000,
            "currency": "eur",
            "success_url": "https://example.com/ok",
            "cancel_url": "https://example.com/cancel",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["checkout_url"] == "https://checkout.stripe.example.com/cs_credits"
    assert captured["mode"] == "payment"
    line_item = captured["line_items"][0]
    assert line_item["quantity"] == 1
    assert line_item["price_data"]["currency"] == "eur"
    # 2,000 credits * 5 cents/credit (CREDIT_RATE_CENTS_PER_CREDIT) = 10,000 cents.
    assert line_item["price_data"]["unit_amount"] == 10_000
    assert line_item["price_data"]["product_data"]["name"] == "2000 tracking credits"
    assert captured["metadata"]["credits"] == "2000"
    assert captured["metadata"]["type"] == "credit_purchase"


def test_credit_checkout_session_without_stripe_configured_returns_503(client, signed_up_org):
    _tokens, headers = signed_up_org
    resp = client.post(
        "/v1/billing/credits/checkout-session",
        json={"credits": 500, "success_url": "https://example.com/ok", "cancel_url": "https://example.com/cancel"},
        headers=headers,
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "feature_not_configured"


def test_credit_checkout_session_rejects_non_positive_credits(client, signed_up_org, monkeypatch):
    _tokens, headers = signed_up_org
    _configure_stripe(monkeypatch)
    resp = client.post(
        "/v1/billing/credits/checkout-session",
        json={"credits": 0, "success_url": "https://example.com/ok", "cancel_url": "https://example.com/cancel"},
        headers=headers,
    )
    assert resp.status_code == 422


# ── Additive credit top-up (webhook-driven) ──────────────────────────────


def test_apply_credit_purchase_tops_up_the_balance_additively_not_a_reset(db_session, api_key):
    """Unlike `refill_credits_for_invoice`/`upsert_subscription_from_stripe_object`
    (which reset the balance to a plan's allotment), a one-off pay-per-credit
    purchase must stack on top of whatever's already there."""
    org_id = _org_id_for(db_session, api_key)
    usage = UsageService()

    usage.charge(
        db_session, organization_id=org_id, api_key_id=None, event_type=UsageEventType.CONTAINER_LOOKUP, credits=2
    )
    balance_before = usage.get_balance(db_session, org_id).credits_remaining  # Free plan: 10 - 2 = 8

    BillingService().apply_credit_purchase(db_session, organization_id=org_id, credits=500)

    assert usage.get_balance(db_session, org_id).credits_remaining == balance_before + 500


def test_webhook_checkout_session_completed_credits_the_organization_additively(client, db_session, api_key, monkeypatch):
    """HTTP-layer test of the new webhook branch (routers/v1/billing.py) -
    bypasses real Stripe signature verification the same way the raw
    request body/signature aren't otherwise exercised in this suite, by
    monkeypatching `construct_webhook_event` directly."""
    org_id = _org_id_for(db_session, api_key)
    usage = UsageService()
    before = usage.get_balance(db_session, org_id).credits_remaining

    fake_event = {
        "type": "checkout.session.completed",
        "data": {
            "object": _FakeStripeObject(
                {"id": "cs_test_credits", "metadata": {"organization_id": str(org_id), "credits": "300", "type": "credit_purchase"}}
            )
        },
    }
    monkeypatch.setattr(BillingService, "construct_webhook_event", lambda self, *, payload, signature: fake_event)

    resp = client.post("/v1/billing/webhook", content=b"{}", headers={"stripe-signature": "test"})
    assert resp.status_code == 200
    assert resp.json() == {"received": True}
    assert usage.get_balance(db_session, org_id).credits_remaining == before + 300


def test_webhook_checkout_session_completed_ignores_non_credit_purchase_sessions(client, db_session, api_key, monkeypatch):
    """A plain (non-credit-purchase) payment-mode checkout - e.g. one this
    product doesn't create today, or metadata missing the type key for any
    other reason - must be a clean noop, not a crash or an accidental credit."""
    org_id = _org_id_for(db_session, api_key)
    usage = UsageService()
    before = usage.get_balance(db_session, org_id).credits_remaining

    fake_event = {
        "type": "checkout.session.completed",
        "data": {"object": _FakeStripeObject({"id": "cs_other", "metadata": {}})},
    }
    monkeypatch.setattr(BillingService, "construct_webhook_event", lambda self, *, payload, signature: fake_event)

    resp = client.post("/v1/billing/webhook", content=b"{}", headers={"stripe-signature": "test"})
    assert resp.status_code == 200
    assert usage.get_balance(db_session, org_id).credits_remaining == before
