"""Tests for workers/tasks/webhook_delivery.py - HMAC signing + the
attempt/backoff/exhaustion state machine. httpx.AsyncClient and the arq
pool are both faked, so this never touches the network or Redis.
"""

from __future__ import annotations

import uuid

import pytest

import app.workers.tasks.webhook_delivery as webhook_delivery_module
from app.core.config import settings
from app.core.security import hash_token, verify_webhook_signature
from app.models.api_key import ApiKey
from app.models.webhook import WebhookDeliveryStatus
from app.repositories.webhooks import WebhookRepository
from app.workers.tasks.webhook_delivery import deliver_webhook

_webhooks = WebhookRepository()


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient - records the outgoing request and
    returns whatever this test configured, instead of making a real call."""

    last_request: dict | None = None
    response: _FakeResponse = _FakeResponse(200)
    raise_exc: Exception | None = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def post(self, url, content=None, headers=None):
        type(self).last_request = {"url": url, "content": content, "headers": headers}
        if type(self).raise_exc is not None:
            raise type(self).raise_exc
        return type(self).response


class _FakeArqPool:
    def __init__(self) -> None:
        self.enqueued: list[tuple] = []

    async def enqueue_job(self, name, *args, **kwargs) -> None:
        self.enqueued.append((name, args, kwargs))


@pytest.fixture()
def fake_pool(monkeypatch) -> _FakeArqPool:
    _FakeAsyncClient.last_request = None
    _FakeAsyncClient.response = _FakeResponse(200)
    _FakeAsyncClient.raise_exc = None
    monkeypatch.setattr(webhook_delivery_module.httpx, "AsyncClient", _FakeAsyncClient)

    pool = _FakeArqPool()

    async def _fake_get_pool():
        return pool

    monkeypatch.setattr(webhook_delivery_module, "get_arq_pool", _fake_get_pool)
    return pool


def _org_id_for(db_session, api_key: str) -> uuid.UUID:
    return db_session.query(ApiKey).filter_by(key_hash=hash_token(api_key)).one().organization_id


def _make_endpoint_and_delivery(db_session, *, organization_id):
    endpoint = _webhooks.create(
        db_session, organization_id=organization_id, url="https://example.com/hook",
        description=None, event_types=["container.updated"],
    )
    delivery = _webhooks.create_delivery(
        db_session, webhook_endpoint_id=endpoint.id, event_type="container.updated",
        payload={"event": "container.updated", "container_number": "MSKU1234567"},
    )
    return endpoint, delivery


@pytest.mark.asyncio
async def test_successful_delivery_is_signed_and_marked_success(db_session, api_key, fake_pool):
    org_id = _org_id_for(db_session, api_key)
    endpoint, delivery = _make_endpoint_and_delivery(db_session, organization_id=org_id)

    await deliver_webhook({}, str(delivery.id))

    # deliver_webhook committed through its own SessionLocal() - without
    # this, db_session's identity map would just hand back the pre-call
    # in-memory object instead of re-querying the row it just changed.
    db_session.expire_all()
    refreshed = _webhooks.get_delivery(db_session, delivery.id)
    assert refreshed.status == WebhookDeliveryStatus.SUCCESS
    assert refreshed.attempt_count == 1
    assert refreshed.response_status_code == 200
    assert fake_pool.enqueued == []  # no retry needed

    sent = _FakeAsyncClient.last_request
    assert sent["headers"]["X-CargoTrack-Event"] == "container.updated"
    assert verify_webhook_signature(payload=sent["content"], signature=sent["headers"]["X-CargoTrack-Signature"], secret=endpoint.secret)


@pytest.mark.asyncio
async def test_failed_delivery_schedules_a_retry_with_backoff(db_session, api_key, fake_pool):
    org_id = _org_id_for(db_session, api_key)
    _endpoint, delivery = _make_endpoint_and_delivery(db_session, organization_id=org_id)
    _FakeAsyncClient.response = _FakeResponse(500, "internal server error")

    await deliver_webhook({}, str(delivery.id))

    # deliver_webhook committed through its own SessionLocal() - without
    # this, db_session's identity map would just hand back the pre-call
    # in-memory object instead of re-querying the row it just changed.
    db_session.expire_all()
    refreshed = _webhooks.get_delivery(db_session, delivery.id)
    assert refreshed.status == WebhookDeliveryStatus.FAILED
    assert refreshed.attempt_count == 1
    assert refreshed.response_status_code == 500
    assert refreshed.next_attempt_at is not None

    assert len(fake_pool.enqueued) == 1
    name, args, kwargs = fake_pool.enqueued[0]
    assert name == "deliver_webhook"
    assert args == (str(delivery.id),)
    assert kwargs["_defer_by"] == 30  # first backoff step


@pytest.mark.asyncio
async def test_delivery_exhausts_after_max_attempts_without_further_retry(db_session, api_key, fake_pool):
    org_id = _org_id_for(db_session, api_key)
    _endpoint, delivery = _make_endpoint_and_delivery(db_session, organization_id=org_id)
    delivery.attempt_count = settings.webhook_max_attempts - 1
    db_session.commit()
    _FakeAsyncClient.response = _FakeResponse(500, "still failing")

    await deliver_webhook({}, str(delivery.id))

    # deliver_webhook committed through its own SessionLocal() - without
    # this, db_session's identity map would just hand back the pre-call
    # in-memory object instead of re-querying the row it just changed.
    db_session.expire_all()
    refreshed = _webhooks.get_delivery(db_session, delivery.id)
    assert refreshed.status == WebhookDeliveryStatus.EXHAUSTED
    assert refreshed.attempt_count == settings.webhook_max_attempts
    assert fake_pool.enqueued == []  # exhausted - no further retry scheduled


@pytest.mark.asyncio
async def test_disabled_endpoint_exhausts_immediately_without_an_http_call(db_session, api_key, fake_pool):
    org_id = _org_id_for(db_session, api_key)
    endpoint, delivery = _make_endpoint_and_delivery(db_session, organization_id=org_id)
    _webhooks.update(db_session, endpoint, is_active=False)

    await deliver_webhook({}, str(delivery.id))

    # deliver_webhook committed through its own SessionLocal() - without
    # this, db_session's identity map would just hand back the pre-call
    # in-memory object instead of re-querying the row it just changed.
    db_session.expire_all()
    refreshed = _webhooks.get_delivery(db_session, delivery.id)
    assert refreshed.status == WebhookDeliveryStatus.EXHAUSTED
    assert _FakeAsyncClient.last_request is None  # never attempted


@pytest.mark.asyncio
async def test_transport_exception_counts_as_a_failed_attempt(db_session, api_key, fake_pool):
    org_id = _org_id_for(db_session, api_key)
    _endpoint, delivery = _make_endpoint_and_delivery(db_session, organization_id=org_id)
    _FakeAsyncClient.raise_exc = ConnectionError("connection refused")

    await deliver_webhook({}, str(delivery.id))

    # deliver_webhook committed through its own SessionLocal() - without
    # this, db_session's identity map would just hand back the pre-call
    # in-memory object instead of re-querying the row it just changed.
    db_session.expire_all()
    refreshed = _webhooks.get_delivery(db_session, delivery.id)
    assert refreshed.status == WebhookDeliveryStatus.FAILED
    assert refreshed.response_status_code is None
    assert "connection refused" in refreshed.response_body_snippet


@pytest.mark.asyncio
async def test_missing_delivery_row_is_a_silent_noop(db_session, api_key, fake_pool):
    await deliver_webhook({}, str(uuid.uuid4()))
    assert _FakeAsyncClient.last_request is None
