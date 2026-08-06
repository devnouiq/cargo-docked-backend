"""Race-safe credit metering.

`try_deduct_credits` relies on the UPDATE's WHERE clause being evaluated
atomically by the database against the row's *current* value, not a value
read moments earlier in Python - two concurrent requests from the same org
each issuing this UPDATE will have the second one's WHERE clause evaluated
against the first one's already-applied decrement (row-level locking during
the UPDATE serializes them), so it's impossible for both to succeed past a
balance that only covers one of them. This is what the old
`Customer.requests_today` counter got wrong: it read the count, checked it
in Python, then wrote count+1 as three separate steps a second concurrent
request could interleave with.
"""

from __future__ import annotations

import uuid

from sqlalchemy import update
from sqlalchemy.orm import Session

from ..models.usage import CreditBalance, UsageEvent, UsageEventType


class UsageRepository:
    def get_or_create_balance(
        self, db: Session, organization_id: uuid.UUID, *, default_credits: int = 0
    ) -> CreditBalance:
        balance = db.query(CreditBalance).filter_by(organization_id=organization_id).one_or_none()
        if balance is None:
            balance = CreditBalance(
                organization_id=organization_id,
                credits_remaining=default_credits,
                credits_included_per_period=default_credits,
            )
            db.add(balance)
            db.flush()
        return balance

    def try_deduct_credits(self, db: Session, organization_id: uuid.UUID, amount: int) -> bool:
        """Atomically deduct `amount` credits iff the org has enough.
        Returns whether the deduction succeeded."""
        result = db.execute(
            update(CreditBalance)
            .where(
                CreditBalance.organization_id == organization_id,
                CreditBalance.credits_remaining >= amount,
            )
            .values(credits_remaining=CreditBalance.credits_remaining - amount)
        )
        db.commit()
        return result.rowcount > 0

    def record_event(
        self,
        db: Session,
        *,
        organization_id: uuid.UUID,
        api_key_id: uuid.UUID | None,
        event_type: UsageEventType,
        credits_charged: int,
        container_number: str | None = None,
        request_metadata: dict | None = None,
    ) -> UsageEvent:
        event = UsageEvent(
            organization_id=organization_id,
            api_key_id=api_key_id,
            event_type=event_type,
            credits_charged=credits_charged,
            container_number=container_number,
            request_metadata=request_metadata or {},
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    def list_events(
        self, db: Session, organization_id: uuid.UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[UsageEvent], int]:
        query = db.query(UsageEvent).filter_by(organization_id=organization_id)
        total = query.count()
        items = query.order_by(UsageEvent.created_at.desc()).limit(limit).offset(offset).all()
        return items, total

    def count_events_since(self, db: Session, organization_id: uuid.UUID, since) -> int:
        return (
            db.query(UsageEvent)
            .filter(UsageEvent.organization_id == organization_id, UsageEvent.created_at >= since)
            .count()
        )
