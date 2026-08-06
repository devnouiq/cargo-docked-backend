"""Webhook subscriptions + a delivery log.

`WebhookEndpoint.secret` is stored in plaintext (not hashed) on purpose:
unlike a password or API key, it's not a bearer credential presented back
to us - we need the raw value on every delivery to compute the HMAC
signature the customer verifies against, so it must be retrievable (same
tradeoff Stripe/GitHub webhook secrets make).

`WebhookDelivery` is append-only per attempt-cycle (one row per event,
mutated as attempts happen) - both the arq worker's retry state and the
audit log a customer's "why didn't I get notified" ticket gets answered
from.
"""

from __future__ import annotations

import enum
import secrets
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base
from .mixins import TimestampMixin, UUIDPrimaryKeyMixin


class WebhookEventType(enum.StrEnum):
    CONTAINER_UPDATED = "container.updated"
    CONTAINER_ARRIVED = "container.arrived"
    CONTAINER_DELAYED = "container.delayed"
    CONTAINER_DISCHARGED = "container.discharged"


class WebhookDeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"  # attempts remain
    EXHAUSTED = "exhausted"  # gave up after webhook_max_attempts


def _generate_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(32)}"


class WebhookEndpoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "webhook_endpoints"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    secret: Mapped[str] = mapped_column(String(100), default=_generate_secret, nullable=False)
    event_types: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    deliveries: Mapped[list["WebhookDelivery"]] = relationship(back_populates="endpoint", cascade="all, delete-orphan")


class WebhookDelivery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "webhook_deliveries"

    webhook_endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False
    )

    event_type: Mapped[WebhookEventType] = mapped_column(Enum(WebhookEventType, native_enum=False, length=30), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[WebhookDeliveryStatus] = mapped_column(
        Enum(WebhookDeliveryStatus, native_enum=False, length=20), default=WebhookDeliveryStatus.PENDING, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body_snippet: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    endpoint: Mapped["WebhookEndpoint"] = relationship(back_populates="deliveries")
