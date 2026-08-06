from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, joinedload

from ..models.container import ContainerEvent, TrackedContainer

if TYPE_CHECKING:
    from ..providers.base import NormalizedTrackingResult


class ContainerRepository:
    def get_or_create(
        self, db: Session, *, organization_id: uuid.UUID, container_number: str, reference: str | None = None,
        carrier_scac: str | None = None,
    ) -> tuple[TrackedContainer, bool]:
        existing = self.get_by_number(db, organization_id=organization_id, container_number=container_number)
        if existing is not None:
            return existing, False
        container = TrackedContainer(
            organization_id=organization_id,
            container_number=container_number.strip().upper(),
            reference=reference,
            carrier_scac=carrier_scac,
        )
        db.add(container)
        db.flush()
        return container, True

    def get_by_number(
        self, db: Session, *, organization_id: uuid.UUID, container_number: str
    ) -> TrackedContainer | None:
        return (
            db.query(TrackedContainer)
            .filter_by(organization_id=organization_id, container_number=container_number.strip().upper())
            .one_or_none()
        )

    def get_with_events(
        self, db: Session, *, organization_id: uuid.UUID, container_number: str
    ) -> TrackedContainer | None:
        return (
            db.query(TrackedContainer)
            .options(joinedload(TrackedContainer.events))
            .filter_by(organization_id=organization_id, container_number=container_number.strip().upper())
            .one_or_none()
        )

    def list_for_org(
        self, db: Session, organization_id: uuid.UUID, *, active_only: bool = True, limit: int = 50, offset: int = 0
    ) -> tuple[list[TrackedContainer], int]:
        query = db.query(TrackedContainer).filter_by(organization_id=organization_id)
        if active_only:
            query = query.filter_by(is_active=True)
        total = query.count()
        items = (
            query.order_by(TrackedContainer.created_at.desc()).limit(limit).offset(offset).all()
        )
        return items, total

    def list_active_for_polling(self, db: Session, *, limit: int = 500) -> list[TrackedContainer]:
        return db.query(TrackedContainer).filter_by(is_active=True).limit(limit).all()

    def deactivate(self, db: Session, container: TrackedContainer) -> None:
        container.is_active = False
        db.flush()

    def apply_provider_result(
        self, db: Session, container: TrackedContainer, *, result: "NormalizedTrackingResult"
    ) -> list[ContainerEvent]:
        """Update summary fields + append any events not already recorded
        (matched by event_code + occurred_at, since providers don't hand
        back stable event IDs we can dedupe on)."""
        container.provider_name = result.provider_name
        container.status = result.status or container.status
        container.last_known_location = result.location or container.last_known_location
        container.vessel = result.vessel or container.vessel
        container.voyage = result.voyage or container.voyage
        container.raw_data = result.raw_data or container.raw_data

        existing_keys = {(e.event_code, e.occurred_at) for e in container.events}
        new_events: list[ContainerEvent] = []
        for normalized_event in result.events:
            key = (normalized_event.event_code, normalized_event.occurred_at)
            if key in existing_keys:
                continue
            event = ContainerEvent(
                container_id=container.id,
                event_code=normalized_event.event_code,
                description=normalized_event.description,
                location=normalized_event.location,
                vessel=normalized_event.vessel,
                voyage=normalized_event.voyage,
                occurred_at=normalized_event.occurred_at,
                is_actual=normalized_event.actual,
                raw_data={
                    "event_code": normalized_event.event_code,
                    "description": normalized_event.description,
                    "location": normalized_event.location,
                    "vessel": normalized_event.vessel,
                    "voyage": normalized_event.voyage,
                    "occurred_at": normalized_event.occurred_at.isoformat() if normalized_event.occurred_at else None,
                    "actual": normalized_event.actual,
                },
            )
            db.add(event)
            new_events.append(event)

        db.flush()
        return new_events
