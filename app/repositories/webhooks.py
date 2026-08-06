from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models.webhook import WebhookDelivery, WebhookDeliveryStatus, WebhookEndpoint


class WebhookRepository:
    def create(
        self, db: Session, *, organization_id: uuid.UUID, url: str, description: str | None, event_types: list[str]
    ) -> WebhookEndpoint:
        endpoint = WebhookEndpoint(
            organization_id=organization_id, url=url, description=description, event_types=event_types
        )
        db.add(endpoint)
        db.commit()
        db.refresh(endpoint)
        return endpoint

    def get(self, db: Session, endpoint_id: uuid.UUID, *, organization_id: uuid.UUID) -> WebhookEndpoint | None:
        return db.query(WebhookEndpoint).filter_by(id=endpoint_id, organization_id=organization_id).one_or_none()

    def list_for_org(self, db: Session, organization_id: uuid.UUID) -> list[WebhookEndpoint]:
        return db.query(WebhookEndpoint).filter_by(organization_id=organization_id).all()

    def list_active_for_event(self, db: Session, organization_id: uuid.UUID, event_type: str) -> list[WebhookEndpoint]:
        endpoints = (
            db.query(WebhookEndpoint)
            .filter_by(organization_id=organization_id, is_active=True)
            .all()
        )
        return [e for e in endpoints if event_type in (e.event_types or [])]

    def update(self, db: Session, endpoint: WebhookEndpoint, **fields) -> WebhookEndpoint:
        for key, value in fields.items():
            if value is not None:
                setattr(endpoint, key, value)
        db.commit()
        db.refresh(endpoint)
        return endpoint

    def delete(self, db: Session, endpoint: WebhookEndpoint) -> None:
        db.delete(endpoint)
        db.commit()

    def create_delivery(
        self, db: Session, *, webhook_endpoint_id: uuid.UUID, event_type: str, payload: dict
    ) -> WebhookDelivery:
        delivery = WebhookDelivery(
            webhook_endpoint_id=webhook_endpoint_id,
            event_type=event_type,
            payload=payload,
            status=WebhookDeliveryStatus.PENDING,
        )
        db.add(delivery)
        db.commit()
        db.refresh(delivery)
        return delivery

    def get_delivery(self, db: Session, delivery_id: uuid.UUID) -> WebhookDelivery | None:
        return db.get(WebhookDelivery, delivery_id)

    def record_attempt(
        self,
        db: Session,
        delivery: WebhookDelivery,
        *,
        status: WebhookDeliveryStatus,
        response_status_code: int | None,
        response_body_snippet: str | None,
        next_attempt_at: datetime | None,
    ) -> WebhookDelivery:
        delivery.attempt_count += 1
        delivery.status = status
        delivery.last_attempted_at = datetime.now(timezone.utc)
        delivery.next_attempt_at = next_attempt_at
        delivery.response_status_code = response_status_code
        delivery.response_body_snippet = response_body_snippet
        db.commit()
        db.refresh(delivery)
        return delivery

    def list_deliveries(self, db: Session, webhook_endpoint_id: uuid.UUID, *, limit: int = 50) -> list[WebhookDelivery]:
        return (
            db.query(WebhookDelivery)
            .filter_by(webhook_endpoint_id=webhook_endpoint_id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit)
            .all()
        )
