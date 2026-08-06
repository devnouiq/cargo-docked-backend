from __future__ import annotations


def test_create_webhook_returns_secret_once(client, api_key):
    resp = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hooks/cargotrack", "event_types": ["container.updated"]},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["secret"].startswith("whsec_")
    assert body["event_types"] == ["container.updated"]


def test_list_webhooks_excludes_secret(client, api_key):
    client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hooks", "event_types": ["container.arrived"]},
        headers={"X-API-Key": api_key},
    )
    resp = client.get("/v1/webhooks", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert "secret" not in resp.json()[0]


def test_update_webhook_event_types(client, api_key):
    created = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hooks", "event_types": ["container.updated"]},
        headers={"X-API-Key": api_key},
    ).json()

    resp = client.patch(
        f"/v1/webhooks/{created['id']}",
        json={"event_types": ["container.delayed", "container.discharged"]},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    assert set(resp.json()["event_types"]) == {"container.delayed", "container.discharged"}


def test_delete_webhook(client, api_key):
    created = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hooks", "event_types": ["container.updated"]},
        headers={"X-API-Key": api_key},
    ).json()

    resp = client.delete(f"/v1/webhooks/{created['id']}", headers={"X-API-Key": api_key})
    assert resp.status_code == 204

    listed = client.get("/v1/webhooks", headers={"X-API-Key": api_key})
    assert listed.json() == []
