from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import app.services.auth_service as auth_service_module


def test_signup_returns_token_pair(client):
    resp = client.post(
        "/v1/auth/signup",
        json={
            "email": "new@example.com",
            "password": "correct-horse-battery-staple",
            "full_name": "New User",
            "organization_name": "New Org",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["expires_in"] > 0


def test_signup_duplicate_email_is_rejected(client):
    payload = {
        "email": "dupe@example.com",
        "password": "correct-horse-battery-staple",
        "full_name": "Dupe",
        "organization_name": "Org A",
    }
    first = client.post("/v1/auth/signup", json=payload)
    assert first.status_code == 200

    second = client.post("/v1/auth/signup", json={**payload, "organization_name": "Org B"})
    assert second.status_code == 409
    assert second.json()["code"] == "conflict"


def test_login_with_wrong_password_is_rejected(client):
    client.post(
        "/v1/auth/signup",
        json={"email": "login@example.com", "password": "correct-horse-battery-staple", "organization_name": "Org"},
    )
    resp = client.post("/v1/auth/login", json={"email": "login@example.com", "password": "wrong-password"})
    assert resp.status_code == 401


def test_me_requires_bearer_token(client):
    resp = client.get("/v1/auth/me")
    assert resp.status_code == 401


def test_me_returns_current_user(client, signed_up_org):
    _tokens, headers = signed_up_org
    resp = client.get("/v1/auth/me", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["email"] == "founder@example.com"


def test_refresh_rotates_token_and_invalidates_old_one(client, signed_up_org):
    tokens, _headers = signed_up_org
    resp = client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 200
    new_tokens = resp.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]

    # The old refresh token was revoked on rotation - reusing it must fail.
    reuse = client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reuse.status_code == 401


def test_logout_revokes_refresh_token(client, signed_up_org):
    tokens, _headers = signed_up_org
    logout_resp = client.post("/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert logout_resp.status_code == 204

    refresh_resp = client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refresh_resp.status_code == 401


def test_switch_organization_reissues_tokens_scoped_to_the_target_org(client, signed_up_org):
    _tokens, founder_headers = signed_up_org

    second_org = client.post(
        "/v1/auth/signup",
        json={"email": "founder@other-org.example.com", "password": "correct-horse-battery-staple", "organization_name": "Second Org"},
    ).json()

    # Add the founder to the second org as a member (using the second org's owner session).
    second_org_headers = {"Authorization": f"Bearer {second_org['access_token']}"}
    added = client.post(
        "/v1/organizations/me/members", json={"email": "founder@example.com", "role": "member"}, headers=second_org_headers
    )
    assert added.status_code == 201
    target_org_id = client.get("/v1/organizations/me", headers=second_org_headers).json()["id"]

    switched = client.post("/v1/auth/switch-organization", json={"organization_id": target_org_id}, headers=founder_headers)
    assert switched.status_code == 200, switched.text
    new_headers = {"Authorization": f"Bearer {switched.json()['access_token']}"}

    org_now = client.get("/v1/organizations/me", headers=new_headers).json()
    assert org_now["id"] == target_org_id


def test_switch_organization_rejects_orgs_the_user_does_not_belong_to(client, signed_up_org):
    _tokens, headers = signed_up_org
    other = client.post(
        "/v1/auth/signup",
        json={"email": "outsider@example.com", "password": "correct-horse-battery-staple", "organization_name": "Outsider Org"},
    ).json()
    other_org_id = client.get(
        "/v1/organizations/me", headers={"Authorization": f"Bearer {other['access_token']}"}
    ).json()["id"]

    resp = client.post("/v1/auth/switch-organization", json={"organization_id": other_org_id}, headers=headers)
    assert resp.status_code == 403


def test_list_my_organizations(client, signed_up_org):
    _tokens, headers = signed_up_org
    resp = client.get("/v1/auth/organizations", headers=headers)
    assert resp.status_code == 200
    orgs = resp.json()
    assert len(orgs) == 1
    assert orgs[0]["slug"] == "acme-shipping"


def _fake_send_password_reset_email(monkeypatch):
    """Stands in for the real Resend HTTP call (services/email_service.py)
    so these tests never hit the network - records every call so a test
    can pull the raw reset token out of the emailed reset_url without a
    dedicated "peek at the DB" backdoor."""
    sent = []
    monkeypatch.setattr(
        auth_service_module.email_service, "send_password_reset_email", lambda **kwargs: sent.append(kwargs)
    )
    return sent


def _token_from_reset_url(reset_url: str) -> str:
    return parse_qs(urlparse(reset_url).query)["token"][0]


def test_forgot_password_returns_204_regardless_of_email_existence(client, signed_up_org, monkeypatch):
    sent = _fake_send_password_reset_email(monkeypatch)

    existing = client.post("/v1/auth/forgot-password", json={"email": "founder@example.com"})
    missing = client.post("/v1/auth/forgot-password", json={"email": "nobody@example.com"})

    assert existing.status_code == 204
    assert missing.status_code == 204
    assert existing.content == missing.content == b""

    # Only the real account actually triggers a reset-token/email - but
    # that's invisible from the two responses above, which is the point.
    assert len(sent) == 1
    assert sent[0]["to_email"] == "founder@example.com"


def test_forgot_password_degrades_gracefully_without_resend_configured(client, signed_up_org):
    """RESEND_API_KEY is force-blanked for the whole suite (conftest.py) -
    forgot-password must still return 204, not surface the
    FeatureNotConfiguredError that email_service raises internally."""
    resp = client.post("/v1/auth/forgot-password", json={"email": "founder@example.com"})
    assert resp.status_code == 204
    assert resp.content == b""


def test_reset_password_with_valid_token_updates_password_and_allows_login(client, signed_up_org, monkeypatch):
    sent = _fake_send_password_reset_email(monkeypatch)

    resp = client.post("/v1/auth/forgot-password", json={"email": "founder@example.com"})
    assert resp.status_code == 204
    assert len(sent) == 1
    token = _token_from_reset_url(sent[0]["reset_url"])

    reset = client.post("/v1/auth/reset-password", json={"token": token, "new_password": "new-correct-horse-battery"})
    assert reset.status_code == 204

    old_login = client.post(
        "/v1/auth/login", json={"email": "founder@example.com", "password": "correct-horse-battery-staple"}
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/v1/auth/login", json={"email": "founder@example.com", "password": "new-correct-horse-battery"}
    )
    assert new_login.status_code == 200


def test_reset_password_with_expired_or_reused_token_is_rejected(client):
    resp = client.post("/v1/auth/reset-password", json={"token": "not-a-real-token", "new_password": "whatever12345"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_reset_password_token_is_single_use(client, signed_up_org, monkeypatch):
    sent = _fake_send_password_reset_email(monkeypatch)
    client.post("/v1/auth/forgot-password", json={"email": "founder@example.com"})
    token = _token_from_reset_url(sent[0]["reset_url"])

    first = client.post("/v1/auth/reset-password", json={"token": token, "new_password": "first-new-password-1"})
    assert first.status_code == 204

    # The token was consumed by the first reset - reusing it must fail,
    # same reuse-after-use pattern as test_refresh_rotates_token_and_invalidates_old_one.
    reuse = client.post("/v1/auth/reset-password", json={"token": token, "new_password": "second-new-password-2"})
    assert reuse.status_code == 401
    assert reuse.json()["code"] == "unauthorized"
