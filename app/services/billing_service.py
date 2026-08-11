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

import uuid
from datetime import datetime, timezone

import stripe
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.errors import FeatureNotConfiguredError, NotFoundError
from ..models.billing import Plan, Subscription, SubscriptionStatus

# The plans this product ships with. Seeded via `seed_default_plans()`
# (called from scripts/init_db.py) - `stripe_price_id` is left None until
# real Stripe Price objects are created and wired in via env; Free needs
# no Stripe object at all, Enterprise is negotiated manually. Starter/
# Growth prices come from settings, not a literal string here, because
# every environment (local dev, staging, prod) has its own Stripe
# account with its own Price IDs - see STRIPE_STARTER_PRICE_ID/
# STRIPE_GROWTH_PRICE_ID in .env.example.
def _default_plans() -> list[dict]:
    return [
        {"code": "free", "name": "Free", "monthly_price_cents": 0, "included_credits": 1_000},
        {"code": "starter", "name": "Starter", "monthly_price_cents": 4_900, "included_credits": 10_000, "stripe_price_id": settings.stripe_starter_price_id},
        {"code": "growth", "name": "Growth", "monthly_price_cents": 19_900, "included_credits": 50_000, "stripe_price_id": settings.stripe_growth_price_id},
        {"code": "enterprise", "name": "Enterprise", "monthly_price_cents": 0, "included_credits": 500_000},
    ]


def _require_stripe() -> None:
    if not settings.stripe_secret_key:
        raise FeatureNotConfiguredError("Billing is not configured on this server (STRIPE_SECRET_KEY is unset).")
    stripe.api_key = settings.stripe_secret_key


def seed_default_plans(db: Session) -> None:
    for plan_data in _default_plans():
        existing = db.query(Plan).filter_by(code=plan_data["code"]).one_or_none()
        if existing is None:
            db.add(Plan(**plan_data))
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

    def create_checkout_session(
        self, db: Session, *, organization_id: uuid.UUID, plan_code: str, success_url: str, cancel_url: str
    ) -> str:
        _require_stripe()
        plan = db.query(Plan).filter_by(code=plan_code).one_or_none()
        if plan is None:
            raise NotFoundError(f"Unknown plan {plan_code!r}.")
        if not plan.stripe_price_id:
            raise FeatureNotConfiguredError(f"Plan {plan_code!r} has no Stripe price configured yet.")

        subscription = self.get_subscription(db, organization_id)
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": plan.stripe_price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            customer=subscription.stripe_customer_id if subscription else None,
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
        )
        return session.url

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
        if subscription is None:
            subscription = Subscription(organization_id=organization_id, plan_id=plan.id)
            db.add(subscription)

        subscription.plan_id = plan.id
        subscription.stripe_customer_id = stripe_subscription["customer"]
        subscription.stripe_subscription_id = stripe_subscription["id"]
        subscription.status = SubscriptionStatus(stripe_subscription["status"])
        db.commit()
        db.refresh(subscription)
        return subscription
