"""Tests for the "queued/in_progress forever, no recovery path" fix
(client-reported bug): arq's job-timeout cancellation (asyncio.CancelledError,
a BaseException) wasn't caught anywhere, so a timed-out or crashed scrape
left a row permanently pending with no way for a customer to force a retry.
Covers both halves of the fix: the immediate cancellation handling, and the
`sweep_stuck_scrapes` safety-net cron for rows already stuck (e.g. from a
worker crash that didn't even raise a catchable exception, or a scrape that
was never actually enqueued).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.core.security import hash_token
from app.models.api_key import ApiKey
from app.models.container import ContainerScrapeStatus, TrackedContainer
from app.repositories.containers import ContainerRepository
from app.workers.tasks.scrape import scrape_container, sweep_stuck_scrapes

_containers = ContainerRepository()


def _org_id_for(db_session, api_key: str) -> uuid.UUID:
    return db_session.query(ApiKey).filter_by(key_hash=hash_token(api_key)).one().organization_id


def _queued_container(db_session, *, organization_id, number: str, updated_at=None) -> TrackedContainer:
    container, _created = _containers.get_or_create(
        db_session, organization_id=organization_id, container_number=number
    )
    container.tracking_status = ContainerScrapeStatus.QUEUED
    if updated_at is not None:
        container.updated_at = updated_at
    db_session.commit()
    return container


def _reload(db_session, container_id: uuid.UUID) -> TrackedContainer:
    db_session.expire_all()
    return db_session.get(TrackedContainer, container_id)


@pytest.mark.asyncio
async def test_cancelled_scrape_is_marked_failed_not_left_hanging(db_session, api_key):
    """Simulates arq's job-timeout cancellation reaching mid-scrape code -
    the row must end up FAILED (with the CancelledError still propagating,
    same as before, so arq's own cancellation bookkeeping is untouched)."""
    org_id = _org_id_for(db_session, api_key)
    container = _queued_container(db_session, organization_id=org_id, number="CANCEL0001")

    with pytest.raises(asyncio.CancelledError):
        await scrape_container({}, str(container.id))

    refreshed = _reload(db_session, container.id)
    assert refreshed.tracking_status == ContainerScrapeStatus.FAILED
    assert refreshed.tracking_message


@pytest.mark.asyncio
async def test_a_row_stuck_past_the_threshold_is_no_longer_treated_as_pending(client, api_key, db_session, fake_arq_pool):
    """A customer's own refresh call must be able to force a fresh attempt
    on a row that's been queued/in_progress far longer than any real scrape
    should take - not wait forever for a job that's never coming."""
    org_id = _org_id_for(db_session, api_key)
    stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.scrape_stuck_threshold_s + 60)
    container = _queued_container(db_session, organization_id=org_id, number="MSKU6666666", updated_at=stale_cutoff)

    resp = client.post("/v1/containers/MSKU6666666/refresh", headers={"X-API-Key": api_key})

    assert resp.status_code == 202, resp.text
    assert len(fake_arq_pool.enqueued) == 1  # a fresh job was actually queued, not treated as already-pending


@pytest.mark.asyncio
async def test_a_recently_queued_row_still_blocks_a_duplicate_refresh(client, api_key, db_session, fake_arq_pool):
    """Sanity check for the above: a row queued moments ago is still
    correctly treated as pending (this isn't a blanket bypass)."""
    org_id = _org_id_for(db_session, api_key)
    _queued_container(db_session, organization_id=org_id, number="MSKU6666666")

    resp = client.post("/v1/containers/MSKU6666666/refresh", headers={"X-API-Key": api_key})

    assert resp.status_code == 202, resp.text
    assert fake_arq_pool.enqueued == []  # no new job - the existing one still owns this row


@pytest.mark.asyncio
async def test_sweep_marks_stale_rows_failed_and_leaves_fresh_ones_alone(db_session, api_key):
    org_id = _org_id_for(db_session, api_key)
    stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.scrape_stuck_threshold_s + 60)
    stuck = _queued_container(db_session, organization_id=org_id, number="MSKU7777770", updated_at=stale_cutoff)
    fresh = _queued_container(db_session, organization_id=org_id, number="MSKU7777771")

    await sweep_stuck_scrapes({})

    assert _reload(db_session, stuck.id).tracking_status == ContainerScrapeStatus.FAILED
    assert _reload(db_session, fresh.id).tracking_status == ContainerScrapeStatus.QUEUED
