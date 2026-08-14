"""Credit metering.

`CreditBalance` is one row per org, mutated via an atomic
UPDATE ... SET credits_remaining = credits_remaining - :n WHERE
credits_remaining >= :n (see services/usage_service.py) - the row itself
is the lock, so concurrent requests from the same org can't both pass a
"do we have enough credits" check and then both deduct (the race the old
`Customer.requests_today` counter had).

`UsageEvent` is an append-only ledger (never updated/deleted) - the audit
trail `GET /v1/usage` reads from and the thing a support ticket ("why was
I charged for this lookup") gets answered against.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base
from .mixins import TimestampMixin, UUIDPrimaryKeyMixin


class UsageEventType(enum.StrEnum):
    CONTAINER_LOOKUP = "container_lookup"
    BULK_LOOKUP = "bulk_lookup"
    # Historical only - the background poller that charged these was removed
    # (auto re-scraping on a timer silently spent user credits). Kept so
    # past usage-event rows of this type still deserialize.
    CONTAINER_REFRESH = "container_refresh"


class CreditBalance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "credit_balances"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    credits_remaining: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credits_included_per_period: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class UsageEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "usage_events"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[UsageEventType] = mapped_column(
        Enum(UsageEventType, native_enum=False, length=30), nullable=False
    )
    credits_charged: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    container_number: Mapped[str | None] = mapped_column(String(30), nullable=True)
    request_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
