"""Account auth: email+password and OAuth (Google), JWT session
issuance/refresh/logout. Dashboard-facing - see routers/v1/__init__.py's
module docstring for the JWT-vs-API-key split rationale.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ...core.config import settings
from ...core.errors import AppError, UnauthorizedError
from ...db.session import get_db
from ...dependencies import CurrentSession, get_current_session, get_current_user
from ...models.user import OAuthProvider, User
from ...schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    OAuthAuthorizeResponse,
    OAuthExchangeRequest,
    RefreshRequest,
    ResetPasswordRequest,
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


@router.post("/forgot-password", status_code=204)
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)) -> None:
    """Always 204, whether or not an account with this email exists -
    see AuthService.forgot_password for why (account-enumeration)."""
    _service.forgot_password(db, email=payload.email)


@router.post("/reset-password", status_code=204)
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)) -> None:
    _service.reset_password(db, token=payload.token, new_password=payload.new_password)


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


def _oauth_redirect_uri(provider: OAuthProvider) -> str:
    """The redirect_uri registered with the provider for this app - now the
    frontend's own callback page, not this backend. Computed from server
    config rather than accepted from the client: Google only ever accepts
    the exact URI registered in the Cloud Console, so trusting a
    client-supplied value here would buy an attacker nothing, but it keeps
    this value out of client control on principle."""
    return f"{settings.frontend_base_url}/auth/{provider.value}/callback"


@router.get("/oauth/{provider}/authorize", response_model=OAuthAuthorizeResponse)
def oauth_authorize(
    provider: OAuthProvider,
    state: str = Query(..., min_length=8, max_length=256),
    code_challenge: str | None = Query(default=None, min_length=43, max_length=128),
) -> OAuthAuthorizeResponse:
    """Builds the provider's hosted authorization URL. `state` and
    `code_challenge` are generated by the frontend (not us) because the
    frontend is what later has to verify the state it gets back and
    present the matching PKCE code_verifier - see oauth_exchange."""
    redirect_uri = _oauth_redirect_uri(provider)
    url = oauth_providers.build_authorization_url(
        provider, redirect_uri=redirect_uri, state=state, code_challenge=code_challenge
    )
    return OAuthAuthorizeResponse(authorization_url=url)


@router.post("/oauth/{provider}/exchange", response_model=TokenPair)
async def oauth_exchange(
    provider: OAuthProvider, payload: OAuthExchangeRequest, request: Request, db: Session = Depends(get_db)
) -> TokenPair:
    """Called by the frontend's own /auth/{provider}/callback page after
    Google redirects there directly - this backend is never in that
    browser round-trip, so Google's consent screen and the browser's
    address bar only ever show our frontend domain, not this Cloud Run
    service. We just verify the authorization code and hand back a JSON
    token pair; no redirects, no browser-visible URLs.

    CSRF `state` verification happens on the frontend (it compares the
    `state` param Google sent back against the value it stashed in
    sessionStorage before redirecting) before this endpoint is ever
    called - this API issues no server-side session, so it has nothing of
    its own to check `payload.state` against.
    """
    redirect_uri = _oauth_redirect_uri(provider)
    try:
        access_token = await oauth_providers.exchange_code_for_access_token(
            provider, code=payload.code, redirect_uri=redirect_uri, code_verifier=payload.code_verifier
        )
        identity = await oauth_providers.fetch_identity(provider, access_token=access_token)
        user, org = _service.oauth_login_or_signup(
            db, provider=provider, provider_account_id=identity.provider_account_id,
            email=identity.email, name=identity.name,
        )
        access, refresh_token, expires_in = _service.issue_tokens(db, user=user, org=org, **_client_context(request))
    except AppError:
        raise
    except Exception:
        logger.exception("oauth exchange failed for provider=%s state=%s", provider.value, payload.state)
        raise UnauthorizedError("Could not complete sign-in with this provider.", code="oauth_exchange_failed")

    return TokenPair(access_token=access, refresh_token=refresh_token, expires_in=expires_in)
