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

import stripe
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.errors import FeatureNotConfiguredError, NotFoundError
from ..models.billing import Plan, Subscription, SubscriptionStatus

# The plans this product ships with. Seeded via `seed_default_plans()`
# (called from scripts/init_db.py) - `stripe_price_id` is left None until
# real Stripe Price objects are created and wired in via env/DB update;
# Free needs no Stripe object at all.
DEFAULT_PLANS: list[dict] = [
    {"code": "free", "name": "Free", "monthly_price_cents": 0, "included_credits": 1_000},
    {"code": "starter", "name": "Starter", "monthly_price_cents": 4_900, "included_credits": 10_000},
    {"code": "growth", "name": "Growth", "monthly_price_cents": 19_900, "included_credits": 50_000},
    {"code": "enterprise", "name": "Enterprise", "monthly_price_cents": 0, "included_credits": 500_000},
]


def _require_stripe() -> None:
    if not settings.stripe_secret_key:
        raise FeatureNotConfiguredError("Billing is not configured on this server (STRIPE_SECRET_KEY is unset).")
    stripe.api_key = settings.stripe_secret_key


def seed_default_plans(db: Session) -> None:
    for plan_data in DEFAULT_PLANS:
        existing = db.query(Plan).filter_by(code=plan_data["code"]).one_or_none()
        if existing is None:
            db.add(Plan(**plan_data))
    db.commit()


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

    def create_billing_portal_session(self, db: Session, *, organization_id: uuid.UUID, return_url: str) -> str:
        _require_stripe()
        subscription = self.get_subscription(db, organization_id)
        if subscription is None or not subscription.stripe_customer_id:
            raise NotFoundError("No billing account found for this organization yet.")
        session = stripe.billing_portal.Session.create(customer=subscription.stripe_customer_id, return_url=return_url)
        return session.url

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
