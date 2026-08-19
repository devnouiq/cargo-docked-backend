"""Usage metering: the single choke point every credit-consuming action
goes through. `charge` is the only way credits leave an org's balance -
container_service.py calls it per lookup/refresh - so `GET /v1/usage`'s
ledger (usage_events) is always a complete, accurate record of what was
charged and why.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from ..core.errors import QuotaExceededError
from ..models.billing import Subscription, SubscriptionStatus
from ..models.usage import CreditBalance, UsageEventType
from ..repositories.usage import UsageRepository

# Subscription states that mean "this org no longer has a live paid plan" -
# Stripe left the row in place (so plan history/invoices stay visible) but
# the org isn't actually entitled to that plan's credits any more.
_LAPSED_SUBSCRIPTION_STATUSES = (
    SubscriptionStatus.CANCELED,
    SubscriptionStatus.PAST_DUE,
    SubscriptionStatus.INCOMPLETE,
)


class UsageService:
    def __init__(self) -> None:
        self.repo = UsageRepository()

    def _quota_exceeded_error(self, db: Session, organization_id: uuid.UUID) -> QuotaExceededError:
        """Distinguishes *why* the org is out of credits so the error is
        actionable instead of a generic 429:
          - never subscribed (still on the one-time free-signup grant,
            which - unlike a paid plan - never renews) -> tell them to
            upgrade, not to "wait for the next billing period" (there
            isn't one).
          - subscribed but the plan lapsed (payment failed / canceled)
            -> tell them the plan itself has expired.
          - subscribed and active, just spent this period's allotment ->
            the only case where "wait for renewal" is actually true.
        """
        subscription = db.query(Subscription).filter_by(organization_id=organization_id).one_or_none()
        if subscription is None:
            return QuotaExceededError(
                "You've used all of your free credits. Upgrade your plan to keep making requests.",
                code="free_credits_exhausted",
            )
        if subscription.status in _LAPSED_SUBSCRIPTION_STATUSES:
            return QuotaExceededError(
                "Your plan has expired. Upgrade your plan to continue making requests.",
                code="plan_expired",
            )
        return QuotaExceededError(
            "Organization has used all credits included in this billing period. "
            "Upgrade your plan for more credits, or wait for the next billing period.",
        )

    def charge(
        self,
        db: Session,
        *,
        organization_id: uuid.UUID,
        api_key_id: uuid.UUID | None,
        event_type: UsageEventType,
        credits: int = 1,
        container_number: str | None = None,
        request_metadata: dict | None = None,
    ) -> None:
        self.repo.get_or_create_balance(db, organization_id)
        deducted = self.repo.try_deduct_credits(db, organization_id, credits)
        if not deducted:
            raise self._quota_exceeded_error(db, organization_id)
        self.repo.record_event(
            db,
            organization_id=organization_id,
            api_key_id=api_key_id,
            event_type=event_type,
            credits_charged=credits,
            container_number=container_number,
            request_metadata=request_metadata,
        )

    def get_balance(self, db: Session, organization_id: uuid.UUID) -> CreditBalance:
        return self.repo.get_or_create_balance(db, organization_id)

    def set_plan_allotment(self, db: Session, organization_id: uuid.UUID, *, included_credits: int) -> CreditBalance:
        return self.repo.set_plan_allotment(db, organization_id, included_credits=included_credits)

    def add_credits(self, db: Session, organization_id: uuid.UUID, credits: int) -> CreditBalance:
        """Additive top-up (pay-per-credit purchase) - NOT a reset like
        `set_plan_allotment`. Ensures the balance row exists first (a brand
        new org with no subscription yet can still buy credits)."""
        self.repo.get_or_create_balance(db, organization_id)
        self.repo.add_credits(db, organization_id, credits)
        return self.repo.get_or_create_balance(db, organization_id)

    def list_events(self, db: Session, organization_id: uuid.UUID, *, limit: int = 50, offset: int = 0):
        return self.repo.list_events(db, organization_id, limit=limit, offset=offset)
