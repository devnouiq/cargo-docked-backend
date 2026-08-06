"""FastAPI auth dependencies.

Two independent auth modes, matching the two audiences this API serves:

  * `get_current_user` / `get_current_organization` - Bearer JWT access
    tokens, issued at login/OAuth/refresh (core/security.py). Used by the
    dashboard-facing routes (org/member/API-key/billing management).
  * `get_api_key_principal` - `X-API-Key` header, hashed and looked up
    against `ApiKey` (repositories/api_keys.py). Used by the public `/v1`
    tracking API - the audience docs/SDKs target, which authenticates
    with a key, not a browser session.

Superseded by this module: the old `get_api_key` that looked up a
plaintext key on a `Customer` row (removed - see app/models/api_key.py).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy.orm import Session

from .core.security import InvalidTokenError, TokenType, decode_token, hash_token
from .db.session import get_db
from .models.api_key import ApiKey
from .models.organization import Organization, OrganizationMember
from .models.user import User
from .repositories.api_keys import ApiKeyRepository

_bearer_scheme = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

_api_key_repository = ApiKeyRepository()


@dataclass(frozen=True)
class CurrentSession:
    user: User
    organization: Organization
    membership: OrganizationMember


@dataclass(frozen=True)
class ApiKeyPrincipal:
    organization: Organization
    api_key: ApiKey


def get_current_session(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
    db: Session = Depends(get_db),
) -> CurrentSession:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    try:
        decoded = decode_token(credentials.credentials, expected_type=TokenType.ACCESS)
        user_id = uuid.UUID(decoded.user_id)
        org_id = uuid.UUID(decoded.org_id)
    except (InvalidTokenError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid access token: {exc}") from exc

    user = db.get(User, user_id)
    organization = db.get(Organization, org_id)
    if user is None or organization is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is no longer valid")

    membership = (
        db.query(OrganizationMember)
        .filter_by(organization_id=organization.id, user_id=user.id)
        .one_or_none()
    )
    if membership is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this organization")

    return CurrentSession(user=user, organization=organization, membership=membership)


def get_current_user(session: CurrentSession = Depends(get_current_session)) -> User:
    return session.user


def get_current_organization(session: CurrentSession = Depends(get_current_session)) -> Organization:
    return session.organization


def get_api_key_principal(
    request: Request,
    api_key_header: str | None = Security(_api_key_header),
    db: Session = Depends(get_db),
) -> ApiKeyPrincipal:
    if not api_key_header:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-API-Key header")

    key = _api_key_repository.get_by_hash(db, hash_token(api_key_header))
    if key is None or not key.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or revoked API key")

    organization = db.get(Organization, key.organization_id)
    if organization is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API key's organization no longer exists")

    _api_key_repository.touch_last_used(db, key)
    request.state.api_key_id = str(key.id)
    request.state.organization_id = str(organization.id)
    return ApiKeyPrincipal(organization=organization, api_key=key)
