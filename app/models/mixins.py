"""Shared column mixins.

UUID primary keys (not autoincrement ints) throughout: this is a
multi-tenant API where primary keys leak into URLs and API responses
(`/v1/containers/{id}`, API key IDs, webhook IDs) - sequential integers
let one customer estimate another's signup/record volume just by
comparing IDs, UUIDs don't.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
