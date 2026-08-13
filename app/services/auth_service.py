"""Signup/login/OAuth/session orchestration.

A new organization always gets `DEFAULT_SIGNUP_CREDITS` free credits on
creation (matches the "Free" plan seeded by services/billing_service.py) -
kept as a plain constant rather than reading the Plan row at signup time,
so signup doesn't fail if billing hasn't been seeded yet in a given
environment.
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..core.errors import ConflictError, ForbiddenError, UnauthorizedError
from ..core.security import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    verify_password,
    InvalidTokenError,
)
from ..core.config import settings
from ..models.auth import PasswordResetToken, RefreshToken
from ..models.organization import Organization, OrganizationMember, OrganizationRole
from ..models.user import OAuthProvider, User
from ..repositories.organizations import OrganizationRepository
from ..repositories.usage import UsageRepository
from ..repositories.users import UserRepository
from . import email_service

logger = logging.getLogger(__name__)

DEFAULT_SIGNUP_CREDITS = 1000


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return base or "org"


class AuthService:
    def __init__(self) -> None:
        self.users = UserRepository()
        self.orgs = OrganizationRepository()
        self.usage = UsageRepository()

    # --- account creation ---------------------------------------------------
    def signup(
        self, db: Session, *, email: str, password: str, full_name: str | None, organization_name: str
    ) -> tuple[User, Organization]:
        if self.users.get_by_email(db, email):
            raise ConflictError("An account with this email already exists.")

        user = self.users.create(db, email=email, full_name=full_name, hashed_password=hash_password(password))
        org = self._create_organization_for(db, name=organization_name, owner=user)
        db.commit()
        db.refresh(user)
        db.refresh(org)

        # Best-effort: a Resend outage or missing RESEND_API_KEY must never
        # fail signup - the account is already committed by this point.
        try:
            email_service.send_welcome_email(to_email=user.email, full_name=user.full_name)
        except Exception:
            logger.warning("failed to send welcome email to user_id=%s", user.id, exc_info=True)

        return user, org

    def _create_organization_for(self, db: Session, *, name: str, owner: User) -> Organization:
        slug = self._unique_slug(db, _slugify(name))
        org = self.orgs.create(db, name=name, slug=slug)
        self.orgs.add_member(db, organization_id=org.id, user_id=owner.id, role=OrganizationRole.OWNER)
        self.usage.get_or_create_balance(db, org.id, default_credits=DEFAULT_SIGNUP_CREDITS)
        return org

    def _unique_slug(self, db: Session, base: str) -> str:
        slug = base
        while self.orgs.get_by_slug(db, slug) is not None:
            slug = f"{base}-{secrets.token_hex(3)}"
        return slug

    # --- email + password -----------------------------------------------------
    def login(self, db: Session, *, email: str, password: str) -> tuple[User, Organization]:
        user = self.users.get_by_email(db, email)
        if user is None or not user.hashed_password or not verify_password(password, user.hashed_password):
            raise UnauthorizedError("Invalid email or password.")
        return user, self._primary_organization(db, user)

    def _primary_organization(self, db: Session, user: User) -> Organization:
        membership = (
            db.query(OrganizationMember)
            .filter_by(user_id=user.id)
            .order_by(OrganizationMember.created_at)
            .first()
        )
        if membership is None:
            raise ConflictError("This user does not belong to any organization.")
        org = self.orgs.get(db, membership.organization_id)
        assert org is not None
        return org

    def list_organizations(self, db: Session, user: User) -> list[Organization]:
        """Every org this user belongs to - what a UI's org switcher lists
        before calling `switch_organization`."""
        memberships = db.query(OrganizationMember).filter_by(user_id=user.id).all()
        return [self.orgs.get(db, m.organization_id) for m in memberships]

    def switch_organization(
        self,
        db: Session,
        *,
        user: User,
        target_organization_id: uuid.UUID,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> tuple[str, str, int]:
        """Re-issue a token pair scoped to a *different* org this same user
        already belongs to. Login/signup always resolve to a user's
        earliest membership (`_primary_organization`) - for anyone who
        belongs to more than one org, this is how they get a session for
        the other one, instead of it being permanently unreachable."""
        membership = self.orgs.get_membership(db, organization_id=target_organization_id, user_id=user.id)
        if membership is None:
            raise ForbiddenError("You are not a member of that organization.")
        org = self.orgs.get(db, target_organization_id)
        assert org is not None
        return self.issue_tokens(db, user=user, org=org, user_agent=user_agent, ip_address=ip_address)

    # --- OAuth -----------------------------------------------------------------
    def oauth_login_or_signup(
        self, db: Session, *, provider: OAuthProvider, provider_account_id: str, email: str | None, name: str | None
    ) -> tuple[User, Organization]:
        user = self.users.get_by_oauth(db, provider=provider, provider_account_id=provider_account_id)
        if user is not None:
            db.commit()
            return user, self._primary_organization(db, user)

        # First time this external identity has signed in - link it to an
        # existing account with the same verified email if one exists,
        # otherwise create a brand-new user + organization.
        user = self.users.get_by_email(db, email) if email else None
        if user is None:
            user = self.users.create(
                db,
                email=email or f"{provider.value}-{provider_account_id}@users.noreply.cargotrack",
                full_name=name,
                hashed_password=None,
            )
            self._create_organization_for(db, name=name or user.email, owner=user)

        self.users.link_oauth_identity(
            db, user_id=user.id, provider=provider, provider_account_id=provider_account_id, email=email
        )
        db.commit()
        db.refresh(user)
        return user, self._primary_organization(db, user)

    # --- token issuance/refresh/revocation --------------------------------------
    def issue_tokens(
        self, db: Session, *, user: User, org: Organization, user_agent: str | None = None, ip_address: str | None = None
    ) -> tuple[str, str, int]:
        access_token = create_access_token(user_id=str(user.id), org_id=str(org.id))
        refresh_token, jti, expires_at = create_refresh_token(user_id=str(user.id), org_id=str(org.id))

        db.add(
            RefreshToken(
                user_id=user.id,
                organization_id=org.id,
                jti_hash=hash_token(jti),
                expires_at=expires_at,
                user_agent=user_agent,
                ip_address=ip_address,
            )
        )
        db.commit()
        return access_token, refresh_token, settings.jwt_access_token_ttl_minutes * 60

    def refresh(self, db: Session, *, refresh_token: str) -> tuple[str, str, int]:
        try:
            decoded = decode_token(refresh_token, expected_type=TokenType.REFRESH)
        except InvalidTokenError as exc:
            raise UnauthorizedError(f"Invalid refresh token: {exc}") from exc

        row = db.query(RefreshToken).filter_by(jti_hash=hash_token(decoded.jti)).one_or_none()
        if row is None or not row.is_active:
            raise UnauthorizedError("Refresh token is invalid, expired, or has been revoked.")

        # Rotate on every use: revoke the presented token so a leaked
        # refresh token that gets replayed after the legitimate client
        # already rotated it is detectably dead, not silently reusable.
        row.revoked_at = datetime.now(timezone.utc)

        user = self.users.get(db, row.user_id)
        org = self.orgs.get(db, row.organization_id)
        if user is None or org is None:
            raise UnauthorizedError("Account no longer exists.")

        return self.issue_tokens(db, user=user, org=org)

    def logout(self, db: Session, *, refresh_token: str) -> None:
        try:
            decoded = decode_token(refresh_token, expected_type=TokenType.REFRESH)
        except InvalidTokenError:
            return
        row = db.query(RefreshToken).filter_by(jti_hash=hash_token(decoded.jti)).one_or_none()
        if row is not None and row.revoked_at is None:
            row.revoked_at = datetime.now(timezone.utc)
            db.commit()

    # --- forgot / reset password -------------------------------------------------
    def forgot_password(self, db: Session, *, email: str) -> None:
        """Always "succeeds" from the caller's perspective - the router's
        /forgot-password always returns 204 regardless of what happens in
        here. Only issues a real reset token when the account exists;
        whether the account exists, whether Resend is configured, and
        whether the send itself succeeds must never be observable from the
        response, or this becomes an account-enumeration oracle.
        """
        user = self.users.get_by_email(db, email)
        if user is None:
            return

        raw_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.password_reset_token_ttl_minutes)
        db.add(PasswordResetToken(user_id=user.id, token_hash=hash_token(raw_token), expires_at=expires_at))
        db.commit()

        reset_url = f"{settings.frontend_base_url}/reset-password?token={raw_token}"
        try:
            email_service.send_password_reset_email(
                to_email=user.email, reset_url=reset_url, ttl_minutes=settings.password_reset_token_ttl_minutes
            )
        except Exception:
            # Same reasoning as signup's welcome email: a Resend outage or
            # missing RESEND_API_KEY must not surface here, and must not
            # change the response the router sends back.
            logger.warning("failed to send password reset email to user_id=%s", user.id, exc_info=True)

    def reset_password(self, db: Session, *, token: str, new_password: str) -> None:
        row = db.query(PasswordResetToken).filter_by(token_hash=hash_token(token)).one_or_none()
        if row is None or not row.is_active:
            raise UnauthorizedError("Reset token is invalid, expired, or has already been used.")

        user = self.users.get(db, row.user_id)
        if user is None:
            raise UnauthorizedError("Reset token is invalid, expired, or has already been used.")

        # Single-use: mark the token spent before anything else can go
        # wrong, same "rotate on use" spirit as refresh-token rotation above.
        row.used_at = datetime.now(timezone.utc)
        user.hashed_password = hash_password(new_password)

        # A password reset means "assume every existing session may be
        # compromised" - kick them all out rather than leaving a session
        # from before the reset (e.g. on a device that triggered the leak)
        # silently valid.
        db.query(RefreshToken).filter_by(user_id=user.id, revoked_at=None).update(
            {RefreshToken.revoked_at: datetime.now(timezone.utc)}
        )
        db.commit()
