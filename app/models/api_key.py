"""API keys are the auth mechanism for the public `/v1` tracking API
(OAuth+JWT is for the dashboard/session). Only `key_hash` is stored - the
raw key is shown to the user exactly once at creation/rotation time (see
core/security.generate_api_key), the same pattern Stripe/GitHub use.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base
from .mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ApiKeyMode(enum.StrEnum):
    LIVE = "live"
    SANDBOX = "sandbox"


class ApiKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "api_keys"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mode: Mapped[ApiKeyMode] = mapped_column(Enum(ApiKeyMode, native_enum=False, length=20), nullable=False)
    # e.g. "ctk_live_" / "ctk_test_" plus a few visible chars - lets the
    # dashboard show "ctk_live_8f2a...", enough for a user to recognize
    # which key is which without ever re-displaying the full secret.
    key_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
