"""Orchestrates the standardized `/v1/containers` API: charge credits,
resolve the container through the provider registry, persist the result,
and notify webhooks when something meaningful changed.

Both `track()` (a direct API call/POST) and `refresh_and_notify()` (called
by the arq poller on its own schedule) end at the same `_apply_result`
step, so "a customer looked it up" and "the background poller checked it"
produce identical downstream behavior (persisted state + webhook events).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.errors import NotFoundError
from ..db.session import SessionLocal
from ..models.container import TrackedContainer
from ..models.usage import UsageEventType
from ..models.webhook import WebhookEventType
from ..providers.base import NormalizedTrackingResult
from ..providers.registry import ProviderRegistry, build_default_registry
from ..repositories.containers import ContainerRepository
from .usage_service import UsageService
from .webhook_service import WebhookService

logger = logging.getLogger(__name__)


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
        self.usage.charge(
            db,
            organization_id=organization_id,
            api_key_id=api_key_id,
            event_type=UsageEventType.CONTAINER_LOOKUP,
            container_number=container_number,
        )

        container, _created = self.containers.get_or_create(
            db, organization_id=organization_id, container_number=container_number,
            reference=reference, carrier_scac=carrier_scac,
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

        if self._is_stale(container.last_polled_at):
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
    def _is_stale(last_polled_at: datetime | None) -> bool:
        if last_polled_at is None:
            return True
        if last_polled_at.tzinfo is None:
            last_polled_at = last_polled_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - last_polled_at > timedelta(seconds=settings.container_cache_ttl_seconds)

    async def refresh_and_notify(self, container_id: uuid.UUID) -> None:
        """Entry point for the arq poller - opens its own session/transaction
        so one container's failure can't roll back another's in the batch."""
        with SessionLocal() as db:
            container = db.get(TrackedContainer, container_id)
            if container is None or not container.is_active:
                return
            self.usage.charge(
                db,
                organization_id=container.organization_id,
                api_key_id=None,
                event_type=UsageEventType.CONTAINER_REFRESH,
                container_number=container.container_number,
            )
            await self._refresh_and_apply(db, container)
            db.commit()

    async def _refresh_and_apply(self, db: Session, container: TrackedContainer) -> None:
        previous_status = container.status
        result = await self.registry.track(container.container_number)

        if not result.ok:
            logger.info("no provider resolved %s: %s", container.container_number, result.error)
            container.raw_data = {**(container.raw_data or {}), "last_error": result.error}
            container.last_polled_at = datetime.now(timezone.utc)
            db.flush()
            return

        new_events = self.containers.apply_provider_result(db, container, result=result)
        container.last_polled_at = datetime.now(timezone.utc)
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
                    "provider": container.provider_name,
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
