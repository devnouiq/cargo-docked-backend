"""Account auth: email+password and OAuth (Google/GitHub), JWT session
issuance/refresh/logout. Dashboard-facing - see routers/v1/__init__.py's
module docstring for the JWT-vs-API-key split rationale.
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ...core.config import settings
from ...core.errors import AppError
from ...db.session import get_db
from ...dependencies import CurrentSession, get_current_session, get_current_user
from ...models.user import OAuthProvider, User
from ...schemas.auth import (
    LoginRequest,
    OAuthAuthorizeResponse,
    RefreshRequest,
    SignupRequest,
    SwitchOrganizationRequest,
    TokenPair,
    UserOut,
)
from ...schemas.organization import OrganizationOut
from ...services import oauth_providers
from ...services.auth_service import AuthService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/auth", tags=["auth"])
_service = AuthService()


def _client_context(request: Request) -> dict:
    return {"user_agent": request.headers.get("user-agent"), "ip_address": request.client.host if request.client else None}


@router.post("/signup", response_model=TokenPair)
def signup(payload: SignupRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    user, org = _service.signup(
        db, email=payload.email, password=payload.password, full_name=payload.full_name,
        organization_name=payload.organization_name,
    )
    access, refresh, expires_in = _service.issue_tokens(db, user=user, org=org, **_client_context(request))
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=expires_in)


@router.post("/login", response_model=TokenPair)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    user, org = _service.login(db, email=payload.email, password=payload.password)
    access, refresh, expires_in = _service.issue_tokens(db, user=user, org=org, **_client_context(request))
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=expires_in)


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)) -> TokenPair:
    access, refresh_token, expires_in = _service.refresh(db, refresh_token=payload.refresh_token)
    return TokenPair(access_token=access, refresh_token=refresh_token, expires_in=expires_in)


@router.post("/logout", status_code=204)
def logout(payload: RefreshRequest, db: Session = Depends(get_db)) -> None:
    _service.logout(db, refresh_token=payload.refresh_token)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.get("/organizations", response_model=list[OrganizationOut])
def list_my_organizations(session: CurrentSession = Depends(get_current_session), db: Session = Depends(get_db)):
    """Every org the current user belongs to - a UI's org-switcher lists
    these and calls POST /v1/auth/switch-organization with the chosen one."""
    return _service.list_organizations(db, session.user)


@router.post("/switch-organization", response_model=TokenPair)
def switch_organization(
    payload: SwitchOrganizationRequest,
    request: Request,
    session: CurrentSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> TokenPair:
    """Re-issues a token pair scoped to a different org the current user is
    already a member of - see AuthService.switch_organization for why this
    exists (login always resolves to a user's *earliest* org membership)."""
    access, refresh_token, expires_in = _service.switch_organization(
        db, user=session.user, target_organization_id=payload.organization_id, **_client_context(request)
    )
    return TokenPair(access_token=access, refresh_token=refresh_token, expires_in=expires_in)


@router.get("/oauth/{provider}/authorize", response_model=OAuthAuthorizeResponse)
def oauth_authorize(provider: OAuthProvider) -> OAuthAuthorizeResponse:
    redirect_uri = f"{settings.oauth_redirect_base_url}/v1/auth/oauth/{provider.value}/callback"
    state = secrets.token_urlsafe(24)
    url = oauth_providers.build_authorization_url(provider, redirect_uri=redirect_uri, state=state)
    return OAuthAuthorizeResponse(authorization_url=url)


@router.get("/oauth/{provider}/callback")
async def oauth_callback(provider: OAuthProvider, code: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    redirect_uri = f"{settings.oauth_redirect_base_url}/v1/auth/oauth/{provider.value}/callback"
    try:
        access_token = await oauth_providers.exchange_code_for_access_token(provider, code=code, redirect_uri=redirect_uri)
        identity = await oauth_providers.fetch_identity(provider, access_token=access_token)
        user, org = _service.oauth_login_or_signup(
            db, provider=provider, provider_account_id=identity.provider_account_id,
            email=identity.email, name=identity.name,
        )
        access, refresh_token, expires_in = _service.issue_tokens(db, user=user, org=org, **_client_context(request))
    except AppError:
        raise
    except Exception:
        logger.exception("oauth callback failed for provider=%s", provider.value)
        error_url = f"{settings.frontend_base_url}/auth/callback?error=oauth_failed"
        return RedirectResponse(error_url)

    # Tokens go in the URL fragment, not the query string: fragments are
    # never sent to the server (or logged by it) on the follow-up request
    # the frontend's SPA router makes, unlike query params.
    fragment = urlencode({"access_token": access, "refresh_token": refresh_token, "expires_in": expires_in})
    return RedirectResponse(f"{settings.frontend_base_url}/auth/callback#{fragment}")
