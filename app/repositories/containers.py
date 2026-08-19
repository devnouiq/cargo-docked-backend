from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, joinedload

from ..models.container import ContainerEvent, TrackedContainer

if TYPE_CHECKING:
    from ..providers.base import NormalizedTrackingResult

_UNKNOWN_VALUES = {"", "unknown", "n/a", "none"}
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _is_meaningful(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in _UNKNOWN_VALUES


def _normalize_occurred_at(occurred_at: datetime | None) -> datetime:
    """Canonical form of `occurred_at` for both sorting *and* as a dedup
    key - SQLite (used in tests) round-trips `DateTime(timezone=True)`
    columns as naive, while Postgres (prod) preserves tzinfo and a
    freshly-built `NormalizedEvent` is always aware (registry.py's
    `_parse_date`). Comparing/keying on the raw values as-is either raises
    "can't compare offset-naive and offset-aware datetimes" (sorting) or
    silently treats the same instant as two different keys (dedup - a
    previously-persisted event re-read as naive would never match an
    incoming aware duplicate). Naive is treated as UTC, matching
    container_service.py's `_is_stale`. A missing timestamp normalizes to
    the oldest possible value."""
    if occurred_at is None:
        return _EPOCH
    if occurred_at.tzinfo is None:
        return occurred_at.replace(tzinfo=timezone.utc)
    return occurred_at


def _most_recent_actual_value(
    events: list[ContainerEvent], field: str, *, exclude: frozenset[str] = frozenset()
) -> str | None:
    """The value of `field` (location/vessel/voyage) from the most recent
    *actual* event that has a meaningful value for it.

    Deliberately never looks at an estimated/future event (`is_actual is
    False`) - a container's `last_known_location`/`vessel`/`voyage` means
    "as of the last confirmed movement", not "wherever the route plan says
    it'll be next". `exclude` (checked case-insensitively) is how
    `apply_provider_result` keeps a vessel name from masquerading as a
    location - some upstream data mixes the two under the same field.
    """
    candidates = [
        e
        for e in events
        if e.is_actual and _is_meaningful(getattr(e, field)) and getattr(e, field).strip().lower() not in exclude
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda e: _normalize_occurred_at(e.occurred_at))
    return getattr(latest, field)


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
        # Most-recently-touched first, not most-recently-created: a
        # re-scraped container should bubble back to the top, and
        # `updated_at` (onupdate=_utcnow) already advances on both
        # creation and every later scrape result/refresh.
        items = (
            query.order_by(TrackedContainer.updated_at.desc()).limit(limit).offset(offset).all()
        )
        return items, total

    def deactivate(self, db: Session, container: TrackedContainer) -> None:
        container.is_active = False
        db.flush()

    def apply_provider_result(
        self, db: Session, container: TrackedContainer, *, result: "NormalizedTrackingResult"
    ) -> list[ContainerEvent]:
        """Update summary fields + append any events not already recorded
        (matched by event_code + occurred_at, since providers don't hand
        back stable event IDs we can dedupe on).

        `last_known_location`/`vessel`/`voyage` are derived from the most
        recent *actual* event (see `_most_recent_actual_value`), not taken
        from the provider's own top-level guess - a provider's `result.
        location`/`vessel`/`voyage` is only used as a fallback for
        event-less providers (the two browser-based ones, which report a
        location but no structured timeline). This is what keeps the field
        from ever showing a future/estimated stop or a stale value once a
        later actual event exists.
        """
        container.status = result.status or container.status
        container.raw_data = result.raw_data or container.raw_data

        existing_events = list(container.events)
        existing_keys = {(e.event_code, _normalize_occurred_at(e.occurred_at)) for e in existing_events}
        new_events: list[ContainerEvent] = []
        for normalized_event in result.events:
            key = (normalized_event.event_code, _normalize_occurred_at(normalized_event.occurred_at))
            if key in existing_keys:
                continue
            # A single provider result can itself contain exact duplicate
            # events (seen in production) - without this, only dupes
            # against *already-persisted* events were caught, not dupes
            # within the same batch.
            existing_keys.add(key)
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

        all_events = existing_events + new_events
        # A vessel name occasionally shows up in an event's `location` field
        # instead of its own `vessel` field (seen in production, e.g. a
        # reefer-monitoring event whose "location" is literally the vessel's
        # name) - exclude any value already known to be a vessel name here
        # so it can't be picked as the location.
        vessel_names = frozenset(e.vessel.strip().lower() for e in all_events if _is_meaningful(e.vessel))

        derived_location = _most_recent_actual_value(all_events, "location", exclude=vessel_names)
        derived_vessel = _most_recent_actual_value(all_events, "vessel")
        derived_voyage = _most_recent_actual_value(all_events, "voyage")

        container.last_known_location = derived_location or result.location or container.last_known_location
        container.vessel = derived_vessel or result.vessel or container.vessel
        container.voyage = derived_voyage or result.voyage or container.voyage

        return new_events
