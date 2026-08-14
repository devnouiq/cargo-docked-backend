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
from ..models.usage import CreditBalance, UsageEventType
from ..repositories.usage import UsageRepository


class UsageService:
    def __init__(self) -> None:
        self.repo = UsageRepository()

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
            raise QuotaExceededError(
                "Organization has insufficient credits for this request. Upgrade your plan or wait for the next billing period."
            )
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

    def list_events(self, db: Session, organization_id: uuid.UUID, *, limit: int = 50, offset: int = 0):
        return self.repo.list_events(db, organization_id, limit=limit, offset=offset)
