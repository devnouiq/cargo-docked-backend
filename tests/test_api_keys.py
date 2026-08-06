from __future__ import annotations


def test_create_api_key_returns_raw_key_once(client, signed_up_org):
    _tokens, headers = signed_up_org
    resp = client.post("/v1/api-keys", json={"name": "prod key", "mode": "live", "scopes": []}, headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["api_key"].startswith("ctk_live_")
    assert body["key_prefix"] in body["api_key"]


def test_list_api_keys_does_not_include_raw_key(client, signed_up_org):
    _tokens, headers = signed_up_org
    client.post("/v1/api-keys", json={"name": "k1", "mode": "sandbox", "scopes": []}, headers=headers)

    resp = client.get("/v1/api-keys", headers=headers)
    assert resp.status_code == 200
    keys = resp.json()
    assert len(keys) == 1
    assert "api_key" not in keys[0]


def test_revoked_api_key_cannot_authenticate(client, signed_up_org):
    _tokens, headers = signed_up_org
    created = client.post("/v1/api-keys", json={"name": "temp", "mode": "sandbox", "scopes": []}, headers=headers).json()

    ok = client.get("/v1/usage", headers={"X-API-Key": created["api_key"]})
    assert ok.status_code == 200

    revoke_resp = client.delete(f"/v1/api-keys/{created['id']}", headers=headers)
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["revoked_at"] is not None

    blocked = client.get("/v1/usage", headers={"X-API-Key": created["api_key"]})
    assert blocked.status_code == 401


def test_rotate_api_key_invalidates_old_key(client, signed_up_org):
    _tokens, headers = signed_up_org
    created = client.post("/v1/api-keys", json={"name": "rotate-me", "mode": "sandbox", "scopes": []}, headers=headers).json()

    rotated = client.post(f"/v1/api-keys/{created['id']}/rotate", headers=headers)
    assert rotated.status_code == 200
    new_key = rotated.json()["api_key"]
    assert new_key != created["api_key"]

    old_key_blocked = client.get("/v1/usage", headers={"X-API-Key": created["api_key"]})
    assert old_key_blocked.status_code == 401

    new_key_works = client.get("/v1/usage", headers={"X-API-Key": new_key})
    assert new_key_works.status_code == 200


def test_missing_api_key_is_rejected(client):
    resp = client.get("/v1/usage")
    assert resp.status_code == 401
