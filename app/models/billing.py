"""Billing: a small local mirror of the Stripe objects we care about.

Stripe stays the source of truth for payment state; these rows exist so
the app can answer "what plan/quota does this org have" without calling
Stripe on every request, kept in sync via the Stripe webhook handler
(routers/v1/billing.py).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base
from .mixins import TimestampMixin, UUIDPrimaryKeyMixin


class SubscriptionStatus(enum.StrEnum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"


class Plan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)  # free, starter, growth, enterprise
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    monthly_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    included_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Null for the Free plan (no Stripe object needed) and for Enterprise
    # (negotiated manually) - both valid, so this stays optional rather
    # than every plan requiring a live Stripe Price.
    stripe_price_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class Subscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id"), nullable=False)

    stripe_customer_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, native_enum=False, length=20), default=SubscriptionStatus.ACTIVE, nullable=False
    )
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    plan: Mapped["Plan"] = relationship()
