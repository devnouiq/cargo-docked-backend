"""Password hashing, JWT issuance/verification, and API-key hashing.

Kept in one module because all three are "prove who you are" primitives
with the same non-negotiable rule: never store the verifiable secret
(password, refresh token, API key) in plaintext - only a hash callers can
be checked against.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

import bcrypt
import jwt

from .config import settings

# bcrypt truncates/errors past 72 bytes - hashing/verifying both slice to
# that limit explicitly rather than relying on the C extension's own
# behavior (which varies by version - some raise, some silently truncate).
_BCRYPT_MAX_BYTES = 72


def _bcrypt_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


# --- passwords ----------------------------------------------------------------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(_bcrypt_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_bcrypt_bytes(password), password_hash.encode("utf-8"))
    except ValueError:
        return False


# --- JWT access/refresh tokens --------------------------------------------------
class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True)
class DecodedToken:
    user_id: str
    org_id: str
    token_type: TokenType
    jti: str
    expires_at: datetime


class InvalidTokenError(Exception):
    pass


def create_access_token(*, user_id: str, org_id: str) -> str:
    return _create_token(
        user_id=user_id,
        org_id=org_id,
        token_type=TokenType.ACCESS,
        ttl=timedelta(minutes=settings.jwt_access_token_ttl_minutes),
    )


def create_refresh_token(*, user_id: str, org_id: str) -> tuple[str, str, datetime]:
    """Returns (token, jti, expires_at) - callers persist `jti` (hashed) so
    the refresh token can be looked up/revoked without storing the raw JWT."""
    jti = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_token_ttl_days)
    token = _create_token(
        user_id=user_id,
        org_id=org_id,
        token_type=TokenType.REFRESH,
        ttl=timedelta(days=settings.jwt_refresh_token_ttl_days),
        jti=jti,
    )
    return token, jti, expires_at


def _create_token(
    *, user_id: str, org_id: str, token_type: TokenType, ttl: timedelta, jti: str | None = None
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user_id,
        "org_id": org_id,
        "type": token_type.value,
        "iat": now,
        "exp": now + ttl,
        "jti": jti or secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, *, expected_type: TokenType) -> DecodedToken:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    if payload.get("type") != expected_type.value:
        raise InvalidTokenError(f"expected token type {expected_type.value!r}, got {payload.get('type')!r}")

    return DecodedToken(
        user_id=payload["sub"],
        org_id=payload["org_id"],
        token_type=TokenType(payload["type"]),
        jti=payload["jti"],
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
    )


def hash_token(raw_token: str) -> str:
    """One-way hash for refresh-token `jti`s / raw secrets we only ever need
    to compare against, never reverse - sha256 is sufficient here (unlike
    passwords, these are high-entropy random tokens, not guessable secrets
    an attacker could brute force offline)."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


# --- API keys -----------------------------------------------------------------
@dataclass(frozen=True)
class GeneratedApiKey:
    raw_key: str  # shown to the user exactly once
    prefix: str  # stored + returned on list/rotate for the user to recognize the key
    key_hash: str  # stored, compared against on every request


def generate_api_key(*, live: bool) -> GeneratedApiKey:
    prefix = settings.api_key_live_prefix if live else settings.api_key_sandbox_prefix
    secret_part = secrets.token_urlsafe(32)
    raw_key = f"{prefix}{secret_part}"
    return GeneratedApiKey(raw_key=raw_key, prefix=prefix, key_hash=hash_token(raw_key))


def verify_webhook_signature(*, payload: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def sign_webhook_payload(*, payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
