from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ..models.container import ContainerScrapeStatus


class ContainerCreateRequest(BaseModel):
    container_number: str = Field(min_length=4, max_length=30)
    reference: str | None = None
    carrier_scac: str | None = Field(
        default=None,
        description=(
            "Optional carrier hint (SCAC-style code, e.g. 'MSCU'), forwarded to the "
            "tracking provider to help resolve the carrier when known. Once the "
            "provider confirms a carrier, the stored value is overwritten with its "
            "resolved code - this field always reflects the best-known carrier, not "
            "necessarily what was originally submitted."
        ),
    )


class ContainerBulkCreateRequest(BaseModel):
    container_numbers: list[str] = Field(min_length=1, max_length=500)


class ContainerEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_code: str
    description: str | None
    location: str | None
    vessel: str | None = Field(
        default=None,
        description="Vessel name at the time of this event. Null when the provider "
        "that resolved this container doesn't report vessel data at the event level "
        "(e.g. Romeu, which has no vessel/voyage concept in its own API).",
    )
    voyage: str | None = Field(
        default=None,
        description="Voyage number at the time of this event. Same nullability caveat as `vessel`.",
    )
    occurred_at: datetime | None
    is_actual: bool


class ContainerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    container_number: str
    reference: str | None
    carrier_scac: str | None = Field(
        default=None,
        description="Carrier SCAC code. Null until the tracking provider resolves a "
        "carrier for this container (or if the customer never supplied one and "
        "resolution is still pending) - once resolved, this reflects the provider's "
        "confirmed carrier, not necessarily what was originally submitted.",
    )
    status: str | None
    last_known_location: str | None
    vessel: str | None = Field(
        default=None,
        description="Vessel name, derived from the most recent confirmed event. Null "
        "until an actual (non-estimated) event with vessel data exists - always null "
        "for a container resolved via Romeu (see the Event object's `vessel` field).",
    )
    voyage: str | None = Field(
        default=None,
        description="Voyage number, same nullability caveats as `vessel`.",
    )
    eta: datetime | None
    etd: datetime | None
    last_free_day: datetime | None
    is_active: bool
    last_polled_at: datetime | None
    # Our own tracking lifecycle - not the carrier's `status` above.
    tracking_status: ContainerScrapeStatus = Field(
        description=(
            "Our own scrape lifecycle for this container (distinct from the carrier's "
            "own free-text `status` above). One of: "
            "`queued` (accepted, a worker will scrape it shortly); "
            "`in_progress` (a scrape is running right now); "
            "`completed` (last scrape resolved real carrier data - see `status`/`events`); "
            "`no_data` (last scrape ran cleanly but the carrier had nothing yet - not an "
            "error, a later refresh may resolve it; see `tracking_message`); "
            "`failed` (the scrape itself errored, e.g. a provider timeout - see "
            "`tracking_message`, and retry via POST .../refresh)."
        )
    )
    tracking_message: str | None = Field(
        default=None,
        description="Human-readable detail, populated only when `tracking_status` is "
        "`no_data` or `failed`; null for every other status (nothing to explain yet).",
    )
    created_at: datetime
    updated_at: datetime


class ContainerDetailOut(ContainerOut):
    events: list[ContainerEventOut] = Field(default_factory=list)


class ContainerBulkResultItem(BaseModel):
    container_number: str
    ok: bool
    container: ContainerOut | None = None
    error: str | None = None


class ContainerBulkResponse(BaseModel):
    results: list[ContainerBulkResultItem]
    queued: int  # accepted and handed to the worker
    rejected: int  # refused up front (quota, validation) - never scraped
    poll_after_seconds: int = Field(
        description="Recommended minimum wait before polling GET /v1/containers/{number} "
        "for these results - same value as this response's Retry-After header, provided "
        "here too for clients that don't easily read response headers. Prefer subscribing "
        "to the container.updated webhook over polling when possible."
    )
