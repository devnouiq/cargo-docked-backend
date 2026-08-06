"""Unit tests for app/core/security.py - no DB, no HTTP client needed."""

from __future__ import annotations

import pytest

from app.core.security import (
    InvalidTokenError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_api_key,
    hash_password,
    hash_token,
    sign_webhook_payload,
    verify_password,
    verify_webhook_signature,
)


def test_hash_password_round_trips():
    hashed = hash_password("correct-horse-battery-staple")
    assert hashed != "correct-horse-battery-staple"
    assert verify_password("correct-horse-battery-staple", hashed)
    assert not verify_password("wrong-password", hashed)


def test_password_longer_than_72_bytes_does_not_crash():
    long_password = "x" * 200
    hashed = hash_password(long_password)
    assert verify_password(long_password, hashed)


def test_access_and_refresh_tokens_are_distinguished():
    access = create_access_token(user_id="u1", org_id="o1")
    refresh, jti, expires_at = create_refresh_token(user_id="u1", org_id="o1")

    decoded_access = decode_token(access, expected_type=TokenType.ACCESS)
    assert decoded_access.user_id == "u1"
    assert decoded_access.org_id == "o1"

    with pytest.raises(InvalidTokenError):
        decode_token(access, expected_type=TokenType.REFRESH)

    decoded_refresh = decode_token(refresh, expected_type=TokenType.REFRESH)
    assert decoded_refresh.jti == jti
    # JWT `exp` is an integer Unix timestamp (seconds) per spec, so the
    # decoded value necessarily loses the sub-second precision the
    # original `expires_at` had before encoding - compare at that
    # resolution rather than for exact equality.
    assert abs((expires_at - decoded_refresh.expires_at).total_seconds()) < 1


def test_generate_api_key_prefix_matches_mode():
    live = generate_api_key(live=True)
    sandbox = generate_api_key(live=False)
    assert live.raw_key.startswith("ctk_live_")
    assert sandbox.raw_key.startswith("ctk_test_")
    assert live.key_hash == hash_token(live.raw_key)


def test_webhook_signature_round_trips():
    payload = b'{"event": "container.updated"}'
    signature = sign_webhook_payload(payload=payload, secret="whsec_test")
    assert verify_webhook_signature(payload=payload, signature=signature, secret="whsec_test")
    assert not verify_webhook_signature(payload=payload, signature=signature, secret="whsec_wrong")
    assert not verify_webhook_signature(payload=payload + b"tampered", signature=signature, secret="whsec_test")
