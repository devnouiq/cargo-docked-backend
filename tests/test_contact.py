"""Contact-form endpoint - public, no auth. RESEND_API_KEY is
force-blanked for the whole suite (conftest.py), so the "happy path"
here is monkeypatching the send (like test_auth.py's password-reset
tests do) and the "unconfigured" path confirms the request degrades to
a clean 503 rather than crashing - see ContactService/email_service for
why this one is allowed to 503 instead of silently swallowing, unlike
welcome/forgot-password email.
"""

from __future__ import annotations

import app.services.contact_service as contact_service_module


def _fake_send_contact_notification(monkeypatch):
    sent = []
    monkeypatch.setattr(
        contact_service_module.email_service,
        "send_contact_notification",
        lambda **kwargs: sent.append(kwargs),
    )
    return sent


def test_valid_submission_succeeds(client, monkeypatch):
    sent = _fake_send_contact_notification(monkeypatch)

    resp = client.post(
        "/v1/contact",
        json={
            "name": "Jane Smith",
            "email": "jane@company.com",
            "company": "Acme Logistics",
            "message": "Tell us what you're working on.",
        },
    )

    assert resp.status_code == 201, resp.text
    assert resp.json() == {"status": "sent"}
    assert len(sent) == 1
    assert sent[0]["name"] == "Jane Smith"
    assert sent[0]["email"] == "jane@company.com"
    assert sent[0]["company"] == "Acme Logistics"


def test_valid_submission_without_company_succeeds(client, monkeypatch):
    sent = _fake_send_contact_notification(monkeypatch)

    resp = client.post(
        "/v1/contact",
        json={"name": "Jane Smith", "email": "jane@company.com", "message": "Hello there."},
    )

    assert resp.status_code == 201, resp.text
    assert sent[0]["company"] is None


def test_invalid_email_returns_422(client, monkeypatch):
    sent = _fake_send_contact_notification(monkeypatch)

    resp = client.post(
        "/v1/contact",
        json={"name": "Jane Smith", "email": "not-an-email", "message": "Hello there."},
    )

    assert resp.status_code == 422
    assert sent == []


def test_missing_required_fields_returns_422(client):
    resp = client.post("/v1/contact", json={"email": "jane@company.com"})
    assert resp.status_code == 422

    resp = client.post("/v1/contact", json={"name": "Jane Smith", "email": "jane@company.com"})
    assert resp.status_code == 422


def test_empty_message_returns_422(client):
    resp = client.post(
        "/v1/contact",
        json={"name": "Jane Smith", "email": "jane@company.com", "message": ""},
    )
    assert resp.status_code == 422


def test_degrades_gracefully_without_resend_configured(client):
    """No monkeypatch here - hits the real email_service.send_contact_notification,
    which raises FeatureNotConfiguredError because RESEND_API_KEY is blanked
    for the whole suite (conftest.py). Must come back as a clean 503, not a 500."""
    resp = client.post(
        "/v1/contact",
        json={"name": "Jane Smith", "email": "jane@company.com", "message": "Hello there."},
    )

    assert resp.status_code == 503
    assert resp.json()["code"] == "feature_not_configured"
