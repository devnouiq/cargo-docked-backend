"""Container tracking data.

`TrackedContainer` + `ContainerEvent` are the new, standardized
`/v1/containers` resource model (per-org, structured milestone timeline).

`ContainerResult` is the **legacy cache table** from before this reorg -
kept unchanged (same table name/columns) because `app/repositories.py`
and the untouched debug stack (`app/routers/searates_debug.py` ->
`app/services/bulk_tracking_service.py`) read/write it directly and must
keep working exactly as before. New code should use `TrackedContainer`/
`ContainerEvent` + `repositories/containers.py` instead; `ContainerResult`
is not extended further.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import Base
from .mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ContainerScrapeStatus(enum.StrEnum):
    """Lifecycle of *our own* attempt to scrape a container - deliberately
    separate from `TrackedContainer.status`, which is the carrier's own text
    ("In Transit"). Never conflate the two.

    `NO_DATA` vs `FAILED` is a product distinction, not a technical one: a
    provider that ran cleanly and simply found nothing yet is a normal,
    non-error outcome (a later manual re-scrape may resolve it), whereas
    `FAILED` is reserved for the scrape job itself crashing.
    """

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_DATA = "no_data"
    FAILED = "failed"


class TrackedContainer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tracked_containers"
    __table_args__ = (UniqueConstraint("organization_id", "container_number", name="uq_org_container"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)

    container_number: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True)  # customer's own PO/booking ref
    carrier_scac: Mapped[str | None] = mapped_column(String(10), nullable=True)

    status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_known_location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    vessel: Mapped[str | None] = mapped_column(String(200), nullable=True)
    voyage: Mapped[str | None] = mapped_column(String(50), nullable=True)
    eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    etd: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_free_day: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # False once DELETE /v1/containers/{n}
    raw_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tracking_status: Mapped[ContainerScrapeStatus] = mapped_column(
        "tracking_status", Enum(ContainerScrapeStatus, native_enum=False, length=20), default=ContainerScrapeStatus.QUEUED, nullable=False
    )
    tracking_message: Mapped[str | None] = mapped_column("tracking_message", String(500), nullable=True)

    events: Mapped[list["ContainerEvent"]] = relationship(
        back_populates="container", cascade="all, delete-orphan", order_by="ContainerEvent.occurred_at"
    )


class ContainerEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "container_events"

    container_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tracked_containers.id", ondelete="CASCADE"), nullable=False)

    event_code: Mapped[str] = mapped_column(String(50), nullable=False)  # gate_in, loaded, departed, discharged, gate_out, empty_returned...
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    vessel: Mapped[str | None] = mapped_column(String(200), nullable=True)
    voyage: Mapped[str | None] = mapped_column(String(50), nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_actual: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # actual vs. estimated
    raw_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    container: Mapped["TrackedContainer"] = relationship(back_populates="events")


class ContainerResult(Base):
    """Legacy scrape-result cache - untouched shape, see module docstring."""

    __tablename__ = "container_results"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    container_number: Mapped[str] = mapped_column(String, index=True, unique=True)
    status: Mapped[str | None] = mapped_column(String)
    location: Mapped[str | None] = mapped_column(String)
    raw_data: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
