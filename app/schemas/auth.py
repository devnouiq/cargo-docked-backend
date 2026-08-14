from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    full_name: str | None = None
    organization_name: str = Field(min_length=1, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=200)


class SwitchOrganizationRequest(BaseModel):
    organization_id: uuid.UUID


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str | None
    created_at: datetime


class OAuthAuthorizeResponse(BaseModel):
    authorization_url: str


class OAuthExchangeRequest(BaseModel):
    code: str
    # Round-tripped from the frontend for audit/log correlation only - the
    # actual CSRF check (comparing this against the value stashed before
    # redirecting to the provider) happens client-side before this request
    # is ever sent, since this API is stateless and never stored the state
    # it handed out. See auth.py's oauth_exchange docstring for why.
    state: str
    code_verifier: str | None = None
