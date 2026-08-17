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

    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)  # free, feeder, panamax, ultra, fleet, enterprise
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    monthly_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # USD variant of monthly_price_cents - nullable for the same reason
    # stripe_price_id_usd is nullable (Free/Enterprise have no Stripe price
    # in either currency; a paid plan may not have a USD price
    # configured in every environment yet). EUR (monthly_price_cents) stays
    # the non-nullable "default" currency column since it's the one every
    # plan has always had.
    monthly_price_cents_usd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    included_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Null for the Free plan (no Stripe object needed) and for Enterprise
    # (negotiated manually) - both valid, so this stays optional rather
    # than every plan requiring a live Stripe Price.
    stripe_price_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # USD counterpart of stripe_price_id - a separate Stripe Price object
    # (Stripe Prices are single-currency), optional until one's created in
    # a given environment's Stripe account and wired in via env
    # (STRIPE_STARTER_PRICE_ID_USD/STRIPE_GROWTH_PRICE_ID_USD).
    stripe_price_id_usd: Mapped[str | None] = mapped_column(String(100), nullable=True)


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
