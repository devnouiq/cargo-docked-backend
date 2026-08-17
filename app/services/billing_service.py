"""Stripe-backed billing.

Every function that actually calls Stripe raises `FeatureNotConfiguredError`
up front if `settings.stripe_secret_key` isn't set, rather than the
`stripe` SDK failing deep inside an HTTP call with a confusing error - the
app must still import/start/serve every other route with no Stripe keys
present at all (e.g. this repo's own dev/test environment).

Local `Plan`/`Subscription` rows are a cache of Stripe's state, kept in
sync by the webhook handler (routers/v1/billing.py) - see
app/models/billing.py.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import stripe
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.errors import FeatureNotConfiguredError, NotFoundError, UpstreamProviderError
from ..models.billing import Plan, Subscription, SubscriptionStatus
from .usage_service import UsageService

logger = logging.getLogger(__name__)

# The plans this product ships with. Seeded via `seed_default_plans()`
# (called from scripts/init_db.py) - `stripe_price_id` is left None until
# real Stripe Price objects are created and wired in via env; Free needs
# no Stripe object at all, Enterprise is negotiated manually. The four
# paid tiers' prices come from settings, not a literal string here,
# because every environment (local dev, staging, prod) has its own
# Stripe account with its own Price IDs - see STRIPE_FEEDER_PRICE_ID etc.
# in .env.example. Superseded the old two-tier Starter/Growth lineup -
# see _RETIRED_PLAN_CODES below and seed_default_plans().
def _default_plans() -> list[dict]:
    return [
        {"code": "free", "name": "Free", "monthly_price_cents": 0, "included_credits": 1_000},
        # USD prices are clean round numbers roughly matching the EUR tiers
        # (~EUR * 1.10-1.12, then rounded to a tidy figure) - not a precise
        # FX conversion, since this product doesn't dynamically reprice by
        # exchange rate; pick a number, keep it stable, adjust manually if
        # it ever drifts too far from real EUR/USD parity.
        {
            "code": "feeder", "name": "Feeder", "monthly_price_cents": 5_000, "monthly_price_cents_usd": 5_500,
            "included_credits": 1_500, "stripe_price_id": settings.stripe_feeder_price_id,
            "stripe_price_id_usd": settings.stripe_feeder_price_id_usd,
        },
        {
            "code": "panamax", "name": "Panamax", "monthly_price_cents": 12_000, "monthly_price_cents_usd": 13_000,
            "included_credits": 4_500, "stripe_price_id": settings.stripe_panamax_price_id,
            "stripe_price_id_usd": settings.stripe_panamax_price_id_usd,
        },
        {
            "code": "ultra", "name": "Ultra", "monthly_price_cents": 23_000, "monthly_price_cents_usd": 25_000,
            "included_credits": 10_000, "stripe_price_id": settings.stripe_ultra_price_id,
            "stripe_price_id_usd": settings.stripe_ultra_price_id_usd,
        },
        {
            "code": "fleet", "name": "Fleet", "monthly_price_cents": 36_000, "monthly_price_cents_usd": 40_000,
            "included_credits": 18_000, "stripe_price_id": settings.stripe_fleet_price_id,
            "stripe_price_id_usd": settings.stripe_fleet_price_id_usd,
        },
        {"code": "enterprise", "name": "Enterprise", "monthly_price_cents": 0, "included_credits": 500_000},
    ]


# Retired plan codes from the old two-tier lineup - seed_default_plans()
# removes these on every run (safe: only ever referenced by a Subscription
# FK, and this product has no way to have live subscriptions on a plan
# code no longer offered in _default_plans() without a human migrating
# them first). Keeping this list explicit rather than "delete anything not
# in _default_plans()" avoids silently deleting a plan someone added by
# hand for a manual/negotiated deal.
_RETIRED_PLAN_CODES = ("starter", "growth")


# Pay-per-credit top-up rate, in cents - same rate applied to both EUR and
# USD for simplicity (this product doesn't track live FX rates; see the
# _default_plans() USD-pricing comment for the same reasoning applied to
# subscription plans). Matches the frontend's PAY_PER_CREDIT_RATE = 0.05
# (src/data/plans.js, src/app/pricing/page.js in the frontend repo).
CREDIT_RATE_CENTS_PER_CREDIT = 5


def _require_stripe() -> None:
    if not settings.stripe_secret_key:
        raise FeatureNotConfiguredError("Billing is not configured on this server (STRIPE_SECRET_KEY is unset).")
    stripe.api_key = settings.stripe_secret_key


def _vat_type_for(vat_number: str) -> str:
    """Stripe requires a specific `type` per tax-id format
    (https://docs.stripe.com/billing/customer/tax-ids#supported-tax-id).
    This is a simple, deliberately non-exhaustive heuristic covering the
    common cases this product expects (EU B2B reverse-charge, plus the
    UK since it looks identical to an EU VAT number but isn't one anymore
    post-Brexit) - not a full country/format validator."""
    prefix = vat_number.strip().upper()[:2]
    if prefix == "GB":
        return "gb_vat"
    return "eu_vat"


def seed_default_plans(db: Session) -> None:
    for plan_data in _default_plans():
        existing = db.query(Plan).filter_by(code=plan_data["code"]).one_or_none()
        if existing is None:
            db.add(Plan(**plan_data))
        else:
            for field, value in plan_data.items():
                setattr(existing, field, value)
    for retired_code in _RETIRED_PLAN_CODES:
        retired = db.query(Plan).filter_by(code=retired_code).one_or_none()
        if retired is None:
            continue
        still_in_use = db.query(Subscription).filter_by(plan_id=retired.id).first() is not None
        if not still_in_use:
            db.delete(retired)
    db.commit()


def _describe_invoice(invoice: "stripe.Invoice") -> str:
    """Stripe invoices have no single human-readable label - derive one
    from the first line item's description, falling back to the
    machine-readable `billing_reason` (e.g. "subscription_cycle")."""
    lines = invoice.lines.data if invoice.lines else []
    if lines and lines[0].description:
        return lines[0].description
    reason = (invoice.billing_reason or "").replace("_", " ").strip()
    return reason.capitalize() if reason else f"Invoice {invoice.number or invoice.id}"


class BillingService:
    def list_plans(self, db: Session) -> list[Plan]:
        return db.query(Plan).order_by(Plan.monthly_price_cents).all()

    def get_subscription(self, db: Session, organization_id: uuid.UUID) -> Subscription | None:
        return db.query(Subscription).filter_by(organization_id=organization_id).one_or_none()

    def get_subscription_by_stripe_id(self, db: Session, stripe_subscription_id: str) -> Subscription | None:
        return db.query(Subscription).filter_by(stripe_subscription_id=stripe_subscription_id).one_or_none()

    def _ensure_customer_with_tax_id(
        self, *, customer_id: str | None, organization_id: uuid.UUID, email: str | None, vat_number: str
    ) -> str:
        """A tax ID must attach to a persisted Stripe Customer, not to a
        not-yet-created one - Checkout normally creates the Customer
        implicitly on first purchase, which is too late for us to attach a
        tax ID to it before the session starts. So when a VAT number is
        supplied, create the Customer explicitly up front (if the org
        doesn't already have one) and attach the tax ID to it here."""
        try:
            if not customer_id:
                customer = stripe.Customer.create(email=email, metadata={"organization_id": str(organization_id)})
                customer_id = customer.id
            stripe.Customer.create_tax_id(customer_id, type=_vat_type_for(vat_number), value=vat_number)
        except stripe.StripeError as exc:
            logger.warning("stripe VAT tax id attach failed for org %s: %s", organization_id, exc)
            raise UpstreamProviderError(f"Could not record VAT number: {exc.user_message or str(exc)}") from exc
        return customer_id

    def create_checkout_session(
        self,
        db: Session,
        *,
        organization_id: uuid.UUID,
        plan_code: str,
        success_url: str,
        cancel_url: str,
        currency: str = "eur",
        vat_number: str | None = None,
        customer_email: str | None = None,
    ) -> str:
        _require_stripe()
        plan = db.query(Plan).filter_by(code=plan_code).one_or_none()
        if plan is None:
            raise NotFoundError(f"Unknown plan {plan_code!r}.")
        price_id = plan.stripe_price_id if currency == "eur" else plan.stripe_price_id_usd
        if not price_id:
            raise FeatureNotConfiguredError(
                f"Plan {plan_code!r} has no Stripe price configured for currency {currency!r} yet."
            )

        subscription = self.get_subscription(db, organization_id)
        customer_id = subscription.stripe_customer_id if subscription else None
        if vat_number:
            customer_id = self._ensure_customer_with_tax_id(
                customer_id=customer_id, organization_id=organization_id, email=customer_email, vat_number=vat_number
            )

        # automatic_tax requires an address to calculate against. A brand-new
        # Checkout-created customer collects one automatically, but an
        # existing `customer_id` (the VAT path above, or a returning
        # subscriber) may have no address on file yet - `customer_update`
        # tells Stripe to save the billing address entered in Checkout back
        # onto the Customer, which is also what lets tax calculation use it.
        # Stripe rejects `customer_update` when no `customer` is set, hence
        # the conditional.
        customer_update = {"address": "auto", "name": "auto"} if customer_id else None
        try:
            session = stripe.checkout.Session.create(
                mode="subscription",
                line_items=[{"price": price_id, "quantity": 1}],
                success_url=success_url,
                cancel_url=cancel_url,
                customer=customer_id,
                customer_update=customer_update,
                client_reference_id=str(organization_id),
                metadata={"organization_id": str(organization_id), "plan_code": plan_code},
                # Stripe does NOT copy the Checkout Session's client_reference_id/
                # metadata onto the Subscription object it creates - the webhook
                # handler reads organization_id off the *subscription*
                # (upsert_subscription_from_stripe_object, keyed by
                # subscription.items.data[0].price.id), so that mapping has to be
                # set here too or every subscription.* webhook after the first
                # checkout is unattributable.
                subscription_data={"metadata": {"organization_id": str(organization_id), "plan_code": plan_code}},
                # Always show the VAT/tax-id field in Checkout, regardless of
                # whether the caller supplied one up front - a customer can
                # still add it in the Stripe-hosted UI. automatic_tax is what
                # actually applies EU B2B reverse-charge once a valid tax ID
                # is on the customer - requires Stripe Tax to be enabled on
                # the connected account; if it isn't (common in a fresh test
                # account), this call fails and surfaces as UpstreamProviderError
                # below rather than silently skipping tax handling.
                tax_id_collection={"enabled": True},
                automatic_tax={"enabled": True},
            )
        except stripe.StripeError as exc:
            # e.g. a plan's stripe_price_id pointing at a Price that doesn't
            # exist (wrong mode, wrong account, deleted/archived), or Stripe
            # Tax not enabled on the account for automatic_tax - Stripe's own
            # message already says exactly what's wrong, so surface it rather
            # than a generic one, but as a typed 502, not a raw 500.
            logger.warning("stripe checkout session creation failed for plan %r: %s", plan_code, exc)
            raise UpstreamProviderError(f"Could not start checkout: {exc.user_message or str(exc)}") from exc
        return session.url

    def create_credit_checkout_session(
        self,
        db: Session,
        *,
        organization_id: uuid.UUID,
        credits: int,
        success_url: str,
        cancel_url: str,
        currency: str = "eur",
        vat_number: str | None = None,
        customer_email: str | None = None,
    ) -> str:
        """One-off pay-per-credit top-up. Unlike subscription checkout,
        there's no fixed Stripe Price per amount (credit counts vary
        continuously) - so this uses Stripe's inline ad-hoc pricing
        (`price_data`) instead of a `price` id, priced at
        CREDIT_RATE_CENTS_PER_CREDIT per credit. `metadata.type =
        "credit_purchase"` is how the webhook handler
        (routers/v1/billing.py) tells this apart from a subscription
        checkout on `checkout.session.completed`."""
        _require_stripe()

        subscription = self.get_subscription(db, organization_id)
        customer_id = subscription.stripe_customer_id if subscription else None
        if vat_number:
            customer_id = self._ensure_customer_with_tax_id(
                customer_id=customer_id, organization_id=organization_id, email=customer_email, vat_number=vat_number
            )

        unit_amount = round(credits * CREDIT_RATE_CENTS_PER_CREDIT)
        # See create_checkout_session - automatic_tax needs an address, and
        # an existing customer_id (the VAT path above) may not have one yet.
        customer_update = {"address": "auto", "name": "auto"} if customer_id else None
        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                line_items=[
                    {
                        "price_data": {
                            "currency": currency,
                            "product_data": {"name": f"{credits} tracking credits"},
                            "unit_amount": unit_amount,
                            # Real (pre-created) Stripe Prices carry a
                            # tax_behavior already (see the plans' Prices -
                            # "exclusive", i.e. tax is added on top of the
                            # listed amount). Ad-hoc price_data has no such
                            # default - automatic_tax needs to be told
                            # explicitly, or it can't determine whether
                            # unit_amount already includes tax.
                            "tax_behavior": "exclusive",
                        },
                        "quantity": 1,
                    }
                ],
                success_url=success_url,
                cancel_url=cancel_url,
                customer=customer_id,
                customer_update=customer_update,
                client_reference_id=str(organization_id),
                metadata={"organization_id": str(organization_id), "credits": str(credits), "type": "credit_purchase"},
                tax_id_collection={"enabled": True},
                automatic_tax={"enabled": True},
            )
        except stripe.StripeError as exc:
            logger.warning("stripe credit checkout session creation failed for org %s: %s", organization_id, exc)
            raise UpstreamProviderError(f"Could not start checkout: {exc.user_message or str(exc)}") from exc
        return session.url

    def apply_credit_purchase(self, db: Session, *, organization_id: uuid.UUID, credits: int) -> None:
        """Called from the `checkout.session.completed` webhook for a
        `type=credit_purchase` session - additively tops up the org's
        credit balance. NOT the same as `refill_credits_for_invoice`/
        `upsert_subscription_from_stripe_object`'s reset-to-allotment
        behavior; a one-off purchase stacks on top of whatever's already
        there."""
        UsageService().add_credits(db, organization_id, credits)

    def _require_customer_id(self, db: Session, organization_id: uuid.UUID) -> str:
        subscription = self.get_subscription(db, organization_id)
        if subscription is None or not subscription.stripe_customer_id:
            raise NotFoundError("No billing account found for this organization yet.")
        return subscription.stripe_customer_id

    def create_billing_portal_session(self, db: Session, *, organization_id: uuid.UUID, return_url: str) -> str:
        _require_stripe()
        customer_id = self._require_customer_id(db, organization_id)
        session = stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url)
        return session.url

    def list_invoices(self, db: Session, *, organization_id: uuid.UUID, limit: int = 12) -> list[dict]:
        _require_stripe()
        customer_id = self._require_customer_id(db, organization_id)
        invoices = stripe.Invoice.list(customer=customer_id, limit=limit)
        return [
            {
                "id": inv.id,
                "number": inv.number,
                "description": _describe_invoice(inv),
                "status": inv.status,
                "amount_cents": inv.total,
                "currency": inv.currency,
                "created_at": datetime.fromtimestamp(inv.created, tz=timezone.utc),
                "hosted_invoice_url": inv.hosted_invoice_url,
                "invoice_pdf": inv.invoice_pdf,
            }
            for inv in invoices.data
        ]

    def get_payment_method(self, db: Session, *, organization_id: uuid.UUID) -> dict | None:
        _require_stripe()
        customer_id = self._require_customer_id(db, organization_id)
        # `.to_dict()` first, not `.get()` on the raw StripeObject - see the
        # webhook handler's note in CLAUDE.md, same SDK quirk applies here.
        customer = stripe.Customer.retrieve(
            customer_id, expand=["invoice_settings.default_payment_method"]
        ).to_dict()
        payment_method = customer.get("invoice_settings", {}).get("default_payment_method")
        if payment_method is None:
            methods = stripe.PaymentMethod.list(customer=customer_id, type="card")
            payment_method = methods.data[0].to_dict() if methods.data else None
        if payment_method is None or payment_method.get("card") is None:
            return None
        card = payment_method["card"]
        return {
            "brand": card["brand"],
            "last4": card["last4"],
            "exp_month": card["exp_month"],
            "exp_year": card["exp_year"],
        }

    def construct_webhook_event(self, *, payload: bytes, signature: str) -> "stripe.Event":
        if not settings.stripe_webhook_secret:
            raise FeatureNotConfiguredError("STRIPE_WEBHOOK_SECRET is not set.")
        return stripe.Webhook.construct_event(payload, signature, settings.stripe_webhook_secret)

    def upsert_subscription_from_stripe_object(
        self, db: Session, *, organization_id: uuid.UUID, stripe_subscription: dict
    ) -> Subscription:
        price_id = stripe_subscription["items"]["data"][0]["price"]["id"]
        plan = db.query(Plan).filter_by(stripe_price_id=price_id).one_or_none()
        if plan is None:
            raise NotFoundError(f"No local plan maps to Stripe price {price_id!r}.")

        subscription = self.get_subscription(db, organization_id)
        # Compare before mutating: a brand-new subscription or a real
        # plan change both refill credits; a same-plan `subscription.updated`
        # (payment method change, cancel_at_period_end toggle, Stripe's
        # at-least-once retry of an event we already processed, ...) must
        # not re-refill and wipe out credits the org already spent this period.
        plan_changed = subscription is None or subscription.plan_id != plan.id
        if subscription is None:
            subscription = Subscription(organization_id=organization_id, plan_id=plan.id)
            db.add(subscription)

        subscription.plan_id = plan.id
        subscription.stripe_customer_id = stripe_subscription["customer"]
        subscription.stripe_subscription_id = stripe_subscription["id"]
        subscription.status = SubscriptionStatus(stripe_subscription["status"])
        db.commit()
        db.refresh(subscription)

        if plan_changed:
            # A customer who just paid for a plan expects its full credit
            # allotment usable immediately, not whatever was left over from
            # their previous plan.
            UsageService().set_plan_allotment(db, organization_id, included_credits=plan.included_credits)

        return subscription

    def refill_credits_for_invoice(self, db: Session, *, invoice: dict) -> None:
        """Reset an org's credit balance to its plan's allotment at the
        start of a new billing period - called on the `invoice.paid`
        webhook, which Stripe fires identically for a monthly renewal and
        an annual renewal (and for the first invoice on a brand-new
        subscription, and for upgrade/downgrade proration invoices - both
        already handled by `upsert_subscription_from_stripe_object`, so
        this being unconditional just sets the same value again there).

        Not gated on `invoice.billing_reason` - the point is "a period
        started, balance = plan.included_credits" regardless of why Stripe
        generated this particular invoice.
        """
        # This API version nests the generating subscription under
        # invoice.parent.subscription_details.subscription rather than a
        # top-level invoice.subscription field (see stripe._invoice.py's
        # Invoice.Parent.SubscriptionDetails) - a one-off invoice has no
        # `parent.subscription_details` at all.
        subscription_id = ((invoice.get("parent") or {}).get("subscription_details") or {}).get("subscription")
        if not subscription_id:
            return

        subscription = self.get_subscription_by_stripe_id(db, subscription_id)
        if subscription is None:
            # Stripe doesn't guarantee webhook delivery order - invoice.paid
            # for a brand-new subscription can arrive before the
            # subscription.created event that creates our local row.
            # subscription.created's own refill covers that case instead.
            logger.warning(
                "invoice.paid for stripe subscription %r with no matching local Subscription row yet",
                subscription_id,
            )
            return

        UsageService().set_plan_allotment(
            db, subscription.organization_id, included_credits=subscription.plan.included_credits
        )
