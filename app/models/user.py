"""A User authenticates via OAuth (Google/GitHub - `OAuthIdentity`) and/or
email+password (`hashed_password`, nullable for OAuth-only accounts).
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base
from .mixins import TimestampMixin, UUIDPrimaryKeyMixin


class OAuthProvider(enum.StrEnum):
    GOOGLE = "google"
    GITHUB = "github"


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Nullable: a user who only ever signs in via OAuth never sets one.
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    memberships: Mapped[list["OrganizationMember"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    oauth_identities: Mapped[list["OAuthIdentity"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class OAuthIdentity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One linked external identity per (provider, provider_account_id) -
    lets a user link both Google and GitHub to the same account, and
    prevents two different users from claiming the same external identity.
    """

    __tablename__ = "oauth_identities"
    __table_args__ = (UniqueConstraint("provider", "provider_account_id", name="uq_oauth_identity"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[OAuthProvider] = mapped_column(Enum(OAuthProvider, native_enum=False, length=20), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email_at_link_time: Mapped[str | None] = mapped_column(String(320), nullable=True)

    user: Mapped["User"] = relationship(back_populates="oauth_identities")
