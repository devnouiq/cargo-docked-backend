"""Minimal OAuth2 authorization-code client for Google - just enough
(authorize URL, code-for-token exchange, userinfo fetch) to run login;
no third-party OAuth SDK dependency for something this small and stable.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..core.config import settings
from ..core.errors import FeatureNotConfiguredError
from ..models.user import OAuthProvider


@dataclass(frozen=True)
class OAuthProviderConfig:
    authorize_url: str
    token_url: str
    userinfo_url: str
    scope: str
    client_id: str | None
    client_secret: str | None


@dataclass(frozen=True)
class OAuthIdentityInfo:
    provider_account_id: str
    email: str | None
    name: str | None


_CONFIGS: dict[OAuthProvider, OAuthProviderConfig] = {
    OAuthProvider.GOOGLE: OAuthProviderConfig(
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        userinfo_url="https://www.googleapis.com/oauth2/v3/userinfo",
        scope="openid email profile",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
    ),
}


def get_config(provider: OAuthProvider) -> OAuthProviderConfig:
    config = _CONFIGS[provider]
    if not config.client_id or not config.client_secret:
        raise FeatureNotConfiguredError(
            f"{provider.value} OAuth is not configured on this server "
            f"(set {provider.value.upper()}_CLIENT_ID / {provider.value.upper()}_CLIENT_SECRET)"
        )
    return config


def build_authorization_url(
    provider: OAuthProvider, *, redirect_uri: str, state: str, code_challenge: str | None = None
) -> str:
    config = get_config(provider)
    params_dict = {
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": config.scope,
        "state": state,
    }
    # PKCE (RFC 7636): the frontend generates code_verifier/code_challenge
    # and only ever sends us the challenge here; the verifier itself is
    # presented later in exchange_code_for_access_token so Google can bind
    # the two together. Optional - degrades to plain authorization-code
    # flow if the caller doesn't pass one.
    if code_challenge:
        params_dict["code_challenge"] = code_challenge
        params_dict["code_challenge_method"] = "S256"
    params = httpx.QueryParams(params_dict)
    return f"{config.authorize_url}?{params}"


async def exchange_code_for_access_token(
    provider: OAuthProvider, *, code: str, redirect_uri: str, code_verifier: str | None = None
) -> str:
    config = get_config(provider)
    data = {
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            config.token_url,
            data=data,
            headers={"Accept": "application/json"},
        )
    response.raise_for_status()
    payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise ValueError(f"{provider.value} token exchange returned no access_token: {payload}")
    return access_token


async def fetch_identity(provider: OAuthProvider, *, access_token: str) -> OAuthIdentityInfo:
    config = get_config(provider)
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(config.userinfo_url, headers=headers)
        response.raise_for_status()
        data = response.json()

    return OAuthIdentityInfo(provider_account_id=data["sub"], email=data.get("email"), name=data.get("name"))
