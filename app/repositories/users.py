from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from ..models.user import OAuthIdentity, OAuthProvider, User


class UserRepository:
    def create(self, db: Session, *, email: str, full_name: str | None, hashed_password: str | None) -> User:
        user = User(email=email.lower(), full_name=full_name, hashed_password=hashed_password)
        db.add(user)
        db.flush()
        return user

    def get(self, db: Session, user_id: uuid.UUID) -> User | None:
        return db.get(User, user_id)

    def get_by_email(self, db: Session, email: str) -> User | None:
        return db.query(User).filter_by(email=email.lower()).one_or_none()

    def get_by_oauth(self, db: Session, *, provider: OAuthProvider, provider_account_id: str) -> User | None:
        identity = (
            db.query(OAuthIdentity)
            .filter_by(provider=provider, provider_account_id=provider_account_id)
            .one_or_none()
        )
        return identity.user if identity else None

    def link_oauth_identity(
        self, db: Session, *, user_id: uuid.UUID, provider: OAuthProvider, provider_account_id: str, email: str | None
    ) -> OAuthIdentity:
        identity = OAuthIdentity(
            user_id=user_id,
            provider=provider,
            provider_account_id=provider_account_id,
            email_at_link_time=email,
        )
        db.add(identity)
        db.flush()
        return identity
