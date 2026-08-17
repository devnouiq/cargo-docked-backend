"""Tests for workers/tasks/scrape.py - the `scrape_container` arq job that
does the work `POST /v1/containers/bulk` no longer does inline.

Two things here are load-bearing product behavior rather than mechanics,
and both have a dedicated test below:
  * a provider that resolves nothing is `no_data`, never `failed`;
  * the job charges no credit - the API already charged when it queued the
    row.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.security import hash_token
from app.models.api_key import ApiKey
from app.models.container import ContainerScrapeStatus, TrackedContainer
from app.repositories.containers import ContainerRepository
from app.workers.tasks.scrape import scrape_container

_containers = ContainerRepository()


def _org_id_for(db_session, api_key: str) -> uuid.UUID:
    return db_session.query(ApiKey).filter_by(key_hash=hash_token(api_key)).one().organization_id


def _queued_container(db_session, *, organization_id, number: str) -> TrackedContainer:
    container, _created = _containers.get_or_create(
        db_session, organization_id=organization_id, container_number=number
    )
    container.tracking_status = ContainerScrapeStatus.QUEUED
    db_session.commit()
    return container


def _reload(db_session, container_id: uuid.UUID) -> TrackedContainer:
    # The task committed through its own SessionLocal() - without expiring,
    # db_session's identity map would hand back the pre-call in-memory
    # object instead of re-querying the row the task just changed.
    db_session.expire_all()
    return db_session.get(TrackedContainer, container_id)


def _credits(client, api_key: str) -> int:
    return client.get("/v1/usage", headers={"X-API-Key": api_key}).json()["credits_remaining"]


@pytest.mark.asyncio
async def test_scrape_applies_provider_result_and_marks_succeeded(db_session, api_key):
    org_id = _org_id_for(db_session, api_key)
    container = _queued_container(db_session, organization_id=org_id, number="MSKU1234567")

    await scrape_container({}, str(container.id))

    refreshed = _reload(db_session, container.id)
    assert refreshed.tracking_status == ContainerScrapeStatus.COMPLETED
    assert refreshed.tracking_message is None
    assert refreshed.status == "In Transit"
    assert refreshed.last_known_location == "Rotterdam"
    assert refreshed.last_polled_at is not None


@pytest.mark.asyncio
async def test_unresolvable_container_is_no_data_not_failed(db_session, api_key):
    """A provider that ran cleanly and found nothing is a normal outcome -
    amber "no data yet", not a red failure. Mirrors the API-level
    test_bulk_tracking_queues_every_number_without_scraping_inline."""
    org_id = _org_id_for(db_session, api_key)
    container = _queued_container(db_session, organization_id=org_id, number="MISS0000001")

    await scrape_container({}, str(container.id))

    refreshed = _reload(db_session, container.id)
    assert refreshed.tracking_status == ContainerScrapeStatus.NO_DATA
    assert refreshed.tracking_status != ContainerScrapeStatus.FAILED
    # Customer-safe, neutral message - never the raw provider error string
    # (that stays server-side, in raw_data["last_error"]).
    assert refreshed.tracking_message == (
        "Container data is not yet available. Try again later or verify the container number is correct."
    )
    assert refreshed.raw_data.get("last_error") == "not found by fake provider"
    assert refreshed.status is None  # nothing to show yet, and nothing blanked out


@pytest.mark.asyncio
async def test_scrape_does_not_charge_a_credit(db_session, api_key, client):
    """The other half of the double-charge guard: the API charged when it
    queued this row, so the worker must not charge again."""
    org_id = _org_id_for(db_session, api_key)
    container = _queued_container(db_session, organization_id=org_id, number="MSKU1234567")

    before = _credits(client, api_key)
    await scrape_container({}, str(container.id))
    assert _credits(client, api_key) == before


@pytest.mark.asyncio
async def test_scrape_marks_failed_without_propagating_when_apply_raises(db_session, api_key, monkeypatch):
    org_id = _org_id_for(db_session, api_key)
    container = _queued_container(db_session, organization_id=org_id, number="MSKU1234567")

    def _boom(*args, **kwargs):
        raise RuntimeError("provider result exploded")

    monkeypatch.setattr(ContainerRepository, "apply_provider_result", _boom)

    # No pytest.raises: the task swallowing this is the assertion.
    await scrape_container({}, str(container.id))

    refreshed = _reload(db_session, container.id)
    assert refreshed.tracking_status == ContainerScrapeStatus.FAILED
    # Customer-safe, neutral message - never the raw exception text (that
    # stays server-side, in raw_data["last_error"]).
    assert refreshed.tracking_message == "We couldn't update this container's tracking status. Please try again shortly."
    assert "provider result exploded" not in refreshed.tracking_message
    assert refreshed.raw_data.get("last_error") == "provider result exploded"


@pytest.mark.asyncio
async def test_scrape_of_a_missing_container_is_a_clean_noop(db_session, api_key):
    await scrape_container({}, str(uuid.uuid4()))  # never existed


@pytest.mark.asyncio
async def test_scrape_of_a_deactivated_container_is_a_clean_noop(db_session, api_key):
    org_id = _org_id_for(db_session, api_key)
    container = _queued_container(db_session, organization_id=org_id, number="MSKU1234567")
    container.is_active = False
    db_session.commit()

    await scrape_container({}, str(container.id))

    refreshed = _reload(db_session, container.id)
    assert refreshed.tracking_status == ContainerScrapeStatus.QUEUED  # untouched
    assert refreshed.status is None
