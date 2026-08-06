from __future__ import annotations


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
