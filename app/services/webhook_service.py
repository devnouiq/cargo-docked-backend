"""Webhook registration + fan-out on domain events.

`trigger()` is called by container_service.py whenever a tracked
container's state changes. It writes one `WebhookDelivery` row per active,
subscribed endpoint (so the audit trail exists even if Redis is briefly
unreachable) and then tries to enqueue the actual HTTP delivery onto the
arq queue (workers/tasks/webhook_delivery.py). If enqueueing fails - no
Redis in this environment, e.g. local dev without `docker compose up` -
the delivery row is left `pending`; nothing crashes the request that
triggered it, and a future retry sweep can pick pending rows back up.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from ..core.errors import NotFoundError
from ..models.webhook import WebhookEndpoint, WebhookEventType
from ..repositories.webhooks import WebhookRepository
from ..workers.redis_pool import get_arq_pool

logger = logging.getLogger(__name__)


class WebhookService:
    def __init__(self) -> None:
        self.repo = WebhookRepository()

    def create(
        self, db: Session, *, organization_id: uuid.UUID, url: str, description: str | None, event_types: list[str]
    ) -> WebhookEndpoint:
        return self.repo.create(db, organization_id=organization_id, url=url, description=description, event_types=event_types)

    def list_for_org(self, db: Session, organization_id: uuid.UUID) -> list[WebhookEndpoint]:
        return self.repo.list_for_org(db, organization_id)

    def update(self, db: Session, *, organization_id: uuid.UUID, endpoint_id: uuid.UUID, **fields) -> WebhookEndpoint:
        endpoint = self.repo.get(db, endpoint_id, organization_id=organization_id)
        if endpoint is None:
            raise NotFoundError("Webhook endpoint not found.")
        return self.repo.update(db, endpoint, **fields)

    def delete(self, db: Session, *, organization_id: uuid.UUID, endpoint_id: uuid.UUID) -> None:
        endpoint = self.repo.get(db, endpoint_id, organization_id=organization_id)
        if endpoint is None:
            raise NotFoundError("Webhook endpoint not found.")
        self.repo.delete(db, endpoint)

    def list_deliveries(self, db: Session, *, organization_id: uuid.UUID, endpoint_id: uuid.UUID):
        endpoint = self.repo.get(db, endpoint_id, organization_id=organization_id)
        if endpoint is None:
            raise NotFoundError("Webhook endpoint not found.")
        return self.repo.list_deliveries(db, endpoint_id)

    async def trigger(
        self, db: Session, *, organization_id: uuid.UUID, event_type: WebhookEventType, payload: dict
    ) -> None:
        endpoints = self.repo.list_active_for_event(db, organization_id, event_type.value)
        if not endpoints:
            return

        pool = None
        for endpoint in endpoints:
            delivery = self.repo.create_delivery(
                db, webhook_endpoint_id=endpoint.id, event_type=event_type.value, payload=payload
            )
            try:
                if pool is None:
                    pool = await get_arq_pool()
                await pool.enqueue_job("deliver_webhook", str(delivery.id))
            except Exception:  # noqa: BLE001 - delivery row already persisted; a sweep can retry
                logger.warning(
                    "failed to enqueue webhook delivery %s for endpoint %s (event=%s) - left pending",
                    delivery.id, endpoint.id, event_type.value, exc_info=True,
                )
