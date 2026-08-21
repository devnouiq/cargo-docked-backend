"""Orchestrates the standardized `/v1/containers` API: charge credits,
resolve the container through the provider registry, persist the result,
and notify webhooks when something meaningful changed.

`track()` (a direct API call/POST) and `process_queued_scrape()` (the
`scrape_container` job queued by `queue_bulk()`/`request_refresh()`) both
end at the same `_refresh_and_apply` step, so "a customer looked it up"
and "the worker drained the queue" produce identical downstream behavior -
persisted state, `tracking_status` transitions and webhook events alike.

The one thing that differs between them is billing: the API layer charges
at submission time, so the worker path must charge nothing (see
`_process_in_own_session`'s `charge_event_type`).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.errors import AppError, NotFoundError
from ..db.session import SessionLocal
from ..models.container import ContainerScrapeStatus, TrackedContainer
from ..models.usage import UsageEventType
from ..models.webhook import WebhookEventType
from ..providers.base import NormalizedTrackingResult
from ..providers.registry import ProviderRegistry, build_default_registry
from ..repositories.containers import ContainerRepository
from ..workers.redis_pool import get_arq_pool
from .usage_service import UsageService
from .webhook_service import WebhookService

logger = logging.getLogger(__name__)

# Customer-safe messages for the two terminal non-success states. Deliberately
# generic - never mention provider names, internal exception classes, or raw
# HTTP client errors (the full diagnostic detail still lands in
# `container.raw_data["last_error"]`, an internal-only field never returned
# by ContainerOut).
_NO_DATA_MESSAGE = "Container data is not yet available. Try again later or verify the container number is correct."
_FAILED_MESSAGE = "We couldn't update this container's tracking status. Please try again shortly."


def _infer_webhook_events(*, previous_status: str | None, result: NormalizedTrackingResult, new_event_codes: list[str]) -> list[WebhookEventType]:
    """Heuristic milestone detection from status text / event codes.

    Deliberately simple for this phase: carriers don't expose a
    standardized milestone taxonomy, so this pattern-matches the English
    status strings/event codes the existing scrapers already produce.
    Tightening this into a real per-carrier mapping is exactly the kind of
    thing a paid data aggregator (see providers/registry.py) would replace.
    """
    events: list[WebhookEventType] = []
    haystack = " ".join(filter(None, [result.status, previous_status, *new_event_codes])).lower()

    if result.status and result.status != previous_status:
        events.append(WebhookEventType.CONTAINER_UPDATED)
    elif new_event_codes:
        events.append(WebhookEventType.CONTAINER_UPDATED)

    if "arriv" in haystack:
        events.append(WebhookEventType.CONTAINER_ARRIVED)
    if "discharg" in haystack:
        events.append(WebhookEventType.CONTAINER_DISCHARGED)
    if "delay" in haystack:
        events.append(WebhookEventType.CONTAINER_DELAYED)

    return list(dict.fromkeys(events))  # de-dupe, preserve order


class ContainerService:
    def __init__(self, registry: ProviderRegistry | None = None) -> None:
        self.registry = registry or build_default_registry()
        self.containers = ContainerRepository()
        self.usage = UsageService()
        self.webhooks = WebhookService()

    async def track(
        self,
        db: Session,
        *,
        organization_id: uuid.UUID,
        api_key_id: uuid.UUID | None,
        container_number: str,
        reference: str | None = None,
        carrier_scac: str | None = None,
    ) -> TrackedContainer:
        """Start tracking a container (`POST /v1/containers`) - but if this
        org is already tracking it, this is the same "search/track" action a
        customer re-runs on a number they've already looked up, so it must
        behave like a read, not a fresh scrape:

          - already `queued`/`in_progress` (e.g. mid-bulk-import) -> a
            worker already owns this scrape; return the row as-is. Charging
            or scraping again here would double-bill and race the worker on
            the same container row.
          - already scraped within `container_cache_ttl_seconds` -> serve
            what's stored, no charge, no live lookup (the caching
            `get_or_refresh` already does for `GET` - this mirrors it here).
          - otherwise (brand new, or stale) -> charge and do one live
            lookup, as before.
        """
        container, created = self.containers.get_or_create(
            db, organization_id=organization_id, container_number=container_number,
            reference=reference, carrier_scac=carrier_scac,
        )
        if not created and not self._needs_live_lookup(container):
            return container

        self.usage.charge(
            db,
            organization_id=organization_id,
            api_key_id=api_key_id,
            event_type=UsageEventType.CONTAINER_LOOKUP,
            container_number=container_number,
        )
        await self._refresh_and_apply(db, container)
        db.commit()
        db.refresh(container)
        return container

    async def get_or_refresh(
        self, db: Session, *, organization_id: uuid.UUID, api_key_id: uuid.UUID | None, container_number: str
    ) -> TrackedContainer:
        """Read path for `GET /v1/containers/{number}`: serves the stored
        row if it was polled within `container_cache_ttl_seconds`, otherwise
        charges a credit and does one live lookup - the same
        cache-then-fetch shape the pre-rework `get_or_track` used, applied
        to the new per-org container model instead of the flat scrape cache.
        """
        container = self.containers.get_by_number(db, organization_id=organization_id, container_number=container_number)
        if container is None:
            raise NotFoundError(
                f"Container {container_number!r} is not being tracked for this organization. "
                "POST /v1/containers to start tracking it."
            )

        if not self._needs_live_lookup(container):
            return container

        self.usage.charge(
            db,
            organization_id=organization_id,
            api_key_id=api_key_id,
            event_type=UsageEventType.CONTAINER_LOOKUP,
            container_number=container_number,
        )
        await self._refresh_and_apply(db, container)
        db.commit()
        db.refresh(container)

        return container

    @staticmethod
    def _is_pending(container: TrackedContainer) -> bool:
        return container.tracking_status in (ContainerScrapeStatus.QUEUED, ContainerScrapeStatus.IN_PROGRESS)

    @classmethod
    def _needs_live_lookup(cls, container: TrackedContainer) -> bool:
        """Whether a read/track call should trigger (and charge for) a live
        provider lookup for this already-tracked container.

        False while a scrape is already `queued`/`in_progress` - a
        background worker (queue_bulk/request_refresh) already owns this
        row's next result, so charging/scraping again here would double-bill
        and race the worker writing to the same row. False again once that
        settles and the row is fresh (`last_polled_at` within
        `container_cache_ttl_seconds`) - only a genuinely stale row needs a
        new live lookup.
        """
        if cls._is_pending(container):
            return False
        return cls._is_stale(container.last_polled_at)

    @staticmethod
    def _is_stale(last_polled_at: datetime | None) -> bool:
        if last_polled_at is None:
            return True
        if last_polled_at.tzinfo is None:
            last_polled_at = last_polled_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - last_polled_at > timedelta(seconds=settings.container_cache_ttl_seconds)

    async def queue_bulk(
        self,
        db: Session,
        *,
        organization_id: uuid.UUID,
        api_key_id: uuid.UUID | None,
        container_numbers: list[str],
    ) -> list[tuple[str, TrackedContainer | None, str | None]]:
        """Async entry point for `POST /v1/containers/bulk`: create the rows,
        charge for them, hand the actual scraping to the arq worker.

        Returns one `(container_number, container_or_None, error_or_None)`
        tuple per *unique* number, in submission order.

        De-duplication happens before any charging: the same number twice in
        one payload is one row, one credit, one job.
        """
        numbers = list(dict.fromkeys(n.strip().upper() for n in container_numbers))

        results: list[tuple[str, TrackedContainer | None, str | None]] = []
        queued: list[TrackedContainer] = []
        for number in numbers:
            try:
                # Charge before create, matching track()'s ordering: a
                # quota-exhausted org must not get a free row.
                self.usage.charge(
                    db,
                    organization_id=organization_id,
                    api_key_id=api_key_id,
                    event_type=UsageEventType.CONTAINER_LOOKUP,
                    container_number=number,
                )
                container, _created = self.containers.get_or_create(
                    db, organization_id=organization_id, container_number=number
                )
                container.tracking_status = ContainerScrapeStatus.QUEUED
                container.tracking_message = None
                db.commit()
            except AppError as exc:  # quota exceeded, etc - isolate this item
                db.rollback()
                results.append((number, None, exc.detail))
                continue
            except Exception as exc:  # noqa: BLE001 - one item's failure must not sink the batch
                db.rollback()
                logger.exception("bulk: failed to queue container %s", number)
                results.append((number, None, str(exc)))
                continue
            results.append((number, container, None))
            queued.append(container)

        await self._enqueue_scrapes(queued)
        return results

    async def request_refresh(
        self, db: Session, *, organization_id: uuid.UUID, api_key_id: uuid.UUID | None, container_number: str
    ) -> TrackedContainer:
        """`POST /v1/containers/{number}/refresh` - queue one more scrape of
        an already-tracked container.

        Idempotent while a scrape is still pending: a double-clicked refresh
        button (or a client re-polling) returns the row as-is rather than
        charging a second credit and enqueueing a duplicate job.
        """
        container = self.containers.get_by_number(
            db, organization_id=organization_id, container_number=container_number
        )
        if container is None or not container.is_active:
            raise NotFoundError(
                f"Container {container_number!r} is not being tracked for this organization. "
                "POST /v1/containers to start tracking it."
            )

        if container.tracking_status in (ContainerScrapeStatus.QUEUED, ContainerScrapeStatus.IN_PROGRESS):
            return container

        self.usage.charge(
            db,
            organization_id=organization_id,
            api_key_id=api_key_id,
            event_type=UsageEventType.CONTAINER_LOOKUP,
            container_number=container.container_number,
        )
        container.tracking_status = ContainerScrapeStatus.QUEUED
        container.tracking_message = None
        db.commit()
        db.refresh(container)

        await self._enqueue_scrapes([container])
        return container

    async def _enqueue_scrapes(self, containers: list[TrackedContainer]) -> None:
        """Fire one `scrape_container` job per container, best-effort.

        Same resilience shape as webhook_service.trigger(): the rows are
        already committed, so a Redis outage here is not an error the caller
        should ever see. Note there is no background sweep anymore (the
        30-minute poller was removed) - a row that fails to enqueue here
        stays `queued` until the customer manually re-scrapes it.
        """
        if not containers:
            return

        pool = None
        for container in containers:
            try:
                if pool is None:
                    pool = await get_arq_pool()
                await pool.enqueue_job("scrape_container", str(container.id))
            except Exception:  # noqa: BLE001 - row already persisted as queued; caller can manually retry
                logger.warning(
                    "failed to enqueue scrape for container %s (%s) - left queued",
                    container.id, container.container_number, exc_info=True,
                )

    async def process_queued_scrape(self, container_id: uuid.UUID) -> None:
        """Entry point for the `scrape_container` arq task.

        Charges nothing on purpose: the API layer already charged when the
        row was queued (queue_bulk/request_refresh). Charging here again is
        the double-billing bug this split exists to prevent.
        """
        await self._process_in_own_session(container_id, charge_event_type=None)

    async def _process_in_own_session(
        self, container_id: uuid.UUID, *, charge_event_type: UsageEventType | None
    ) -> None:
        """Opens its own session/transaction so one container's failure can't
        roll back another's in the same batch."""
        with SessionLocal() as db:
            container = db.get(TrackedContainer, container_id)
            if container is None or not container.is_active:
                return
            if charge_event_type is not None:
                self.usage.charge(
                    db,
                    organization_id=container.organization_id,
                    api_key_id=None,
                    event_type=charge_event_type,
                    container_number=container.container_number,
                )

            # Commit IN_PROGRESS before the (slow) provider call so a client
            # polling mid-scrape sees it, rather than a stale `queued`.
            container.tracking_status = ContainerScrapeStatus.IN_PROGRESS
            db.commit()

            try:
                await self._refresh_and_apply(db, container)
                db.commit()
            except Exception as exc:
                # The rollback expires `container`; re-fetch before writing
                # the terminal status or we'd just touch a detached object.
                db.rollback()
                failed = db.get(TrackedContainer, container_id)
                if failed is not None:
                    failed.tracking_status = ContainerScrapeStatus.FAILED
                    # Customer-safe message only - the raw exception (which may
                    # name an internal exception class or an HTTP client error)
                    # stays server-side: it's re-raised below, and the arq task
                    # (workers/tasks/scrape.py) logs it via logger.exception.
                    failed.tracking_message = _FAILED_MESSAGE
                    failed.raw_data = {**(failed.raw_data or {}), "last_error": str(exc)[:500]}
                    db.commit()
                raise

    async def _refresh_and_apply(self, db: Session, container: TrackedContainer) -> None:
        previous_status = container.status
        result = await self.registry.track(container.container_number)

        if not result.ok:
            # A provider that ran cleanly and found nothing is NO_DATA, not
            # FAILED - "tracked, no data yet" is a normal state, and the
            # previously-known status/location deliberately stay put.
            logger.info("no provider resolved %s: %s", container.container_number, result.error)
            container.raw_data = {**(container.raw_data or {}), "last_error": result.error}
            container.last_polled_at = datetime.now(timezone.utc)
            container.tracking_status = ContainerScrapeStatus.NO_DATA
            # Customer-safe, provider-agnostic message - the raw diagnostic
            # (which may name a provider or an internal error string) stays
            # in raw_data, an internal-only field ContainerOut never returns.
            container.tracking_message = _NO_DATA_MESSAGE
            db.flush()
            return

        new_events = self.containers.apply_provider_result(db, container, result=result)
        container.last_polled_at = datetime.now(timezone.utc)
        container.tracking_status = ContainerScrapeStatus.COMPLETED
        container.tracking_message = None
        db.flush()

        event_types = _infer_webhook_events(
            previous_status=previous_status, result=result, new_event_codes=[e.event_code for e in new_events]
        )
        for event_type in event_types:
            await self.webhooks.trigger(
                db,
                organization_id=container.organization_id,
                event_type=event_type,
                payload={
                    "event": event_type.value,
                    "container_number": container.container_number,
                    "status": container.status,
                    "location": container.last_known_location,
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                },
            )

    async def stop_tracking(self, db: Session, *, organization_id: uuid.UUID, container_number: str) -> bool:
        container = self.containers.get_by_number(db, organization_id=organization_id, container_number=container_number)
        if container is None or not container.is_active:
            return False
        self.containers.deactivate(db, container)
        db.commit()
        return True
