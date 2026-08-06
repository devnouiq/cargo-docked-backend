"""Refresh tokens are tracked server-side (unlike access tokens, which are
short-lived and verified stateless) so a single session can be revoked on
logout/"sign out everywhere" without waiting out the token's TTL, and so a
stolen refresh token can be detected reused after revocation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base
from .mixins import TimestampMixin, UUIDPrimaryKeyMixin


class RefreshToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # sha256 of the JWT's `jti` claim - never the raw token, so a DB leak
    # doesn't hand out valid refresh tokens directly (see core/security.hash_token).
    jti_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    @property
    def is_active(self) -> bool:
        # SQLite doesn't reliably round-trip tzinfo through DateTime(timezone=True)
        # - a row read back can come back naive even though it was written aware.
        # Treat a naive value as UTC rather than letting the comparison below
        # raise on aware-vs-naive.
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return self.revoked_at is None and expires_at > datetime.now(timezone.utc)
