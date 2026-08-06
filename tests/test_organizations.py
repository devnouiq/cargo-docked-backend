"""Org/member management - JWT-authenticated, owner/admin-gated writes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def _signup(client, email, org_name):
    resp = client.post(
        "/v1/auth/signup",
        json={"email": email, "password": "correct-horse-battery-staple", "organization_name": org_name},
    )
    assert resp.status_code == 200, resp.text
    tokens = resp.json()
    return tokens, {"Authorization": f"Bearer {tokens['access_token']}"}


def test_signup_owner_is_sole_initial_member(client, signed_up_org):
    _tokens, headers = signed_up_org
    resp = client.get("/v1/organizations/me/members", headers=headers)
    assert resp.status_code == 200
    members = resp.json()
    assert len(members) == 1
    assert members[0]["role"] == "owner"


def test_add_member_requires_the_invitee_to_already_have_an_account(client, signed_up_org):
    _tokens, headers = signed_up_org
    resp = client.post("/v1/organizations/me/members", json={"email": "nobody@example.com"}, headers=headers)
    assert resp.status_code == 404


def test_owner_can_add_an_existing_user_as_a_member(client, signed_up_org):
    _tokens, headers = signed_up_org
    _teammate_tokens, _teammate_headers = _signup(client, "teammate@example.com", "Teammate's Own Org")

    resp = client.post(
        "/v1/organizations/me/members", json={"email": "teammate@example.com", "role": "member"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "member"

    members = client.get("/v1/organizations/me/members", headers=headers).json()
    assert len(members) == 2


def test_adding_the_same_member_twice_conflicts(client, signed_up_org):
    _tokens, headers = signed_up_org
    _signup(client, "teammate2@example.com", "Another Org")

    first = client.post("/v1/organizations/me/members", json={"email": "teammate2@example.com"}, headers=headers)
    assert first.status_code == 201
    second = client.post("/v1/organizations/me/members", json={"email": "teammate2@example.com"}, headers=headers)
    assert second.status_code == 409


def test_non_manager_cannot_add_members(client, signed_up_org, db_session):
    from app.models.organization import OrganizationMember

    owner_tokens, owner_headers = signed_up_org
    _signup(client, "member-only@example.com", "Member Org")
    added = client.post(
        "/v1/organizations/me/members", json={"email": "member-only@example.com", "role": "member"}, headers=owner_headers
    ).json()

    # Every signup makes the signer owner of their own new org, so
    # member-only@example.com already has an earlier "primary" membership
    # (their own org) than the one just added here - login resolves to
    # a user's *earliest* membership (see AuthService._primary_organization),
    # and this app has no "switch active org" endpoint yet. Back-date this
    # membership so it's the one login picks, to actually exercise the
    # permission check against the founder's org rather than their own.
    membership = db_session.get(OrganizationMember, uuid.UUID(added["id"]))
    membership.created_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    db_session.commit()

    # Log in as the newly-added (non-manager) member and try to add someone else.
    member_login = client.post(
        "/v1/auth/login", json={"email": "member-only@example.com", "password": "correct-horse-battery-staple"}
    ).json()
    member_headers = {"Authorization": f"Bearer {member_login['access_token']}"}

    _signup(client, "third@example.com", "Third Org")
    resp = client.post("/v1/organizations/me/members", json={"email": "third@example.com"}, headers=member_headers)
    assert resp.status_code == 403
    assert added["role"] == "member"


def test_cannot_remove_the_last_owner(client, signed_up_org):
    _tokens, headers = signed_up_org
    members = client.get("/v1/organizations/me/members", headers=headers).json()
    owner_id = members[0]["user_id"]

    resp = client.delete(f"/v1/organizations/me/members/{owner_id}", headers=headers)
    assert resp.status_code == 409


def test_update_member_role(client, signed_up_org):
    _tokens, headers = signed_up_org
    _signup(client, "promote-me@example.com", "Promote Org")
    member = client.post(
        "/v1/organizations/me/members", json={"email": "promote-me@example.com", "role": "member"}, headers=headers
    ).json()

    resp = client.patch(f"/v1/organizations/me/members/{member['user_id']}", json={"role": "admin"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"


def test_organizations_route_requires_jwt_not_api_key(client, api_key):
    resp = client.get("/v1/organizations/me", headers={"X-API-Key": api_key})
    assert resp.status_code == 401
