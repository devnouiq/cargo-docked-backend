"""Typed response shapes for the free/unauthenticated tracking preview
(routers/searates_debug.py). Split out of one flattened dict (GoCometTracker.
_parse()'s return value - see providers/gocomet_http.py) into focused
payloads per data type, so each route has a real OpenAPI/Swagger schema
instead of an untyped `dict`:

- `TrackingSummaryOut`: container identity/status/carrier/route summary.
- `TrackingEventOut` / `TrackingEventsOut`: the milestone timeline, its own
  endpoint since it can be long and callers checking status alone don't
  need it.

Internal scrape diagnostics (`create_call_s`/`poll_call_s`/`total_call_s`)
are deliberately not part of either shape - they're ops/debugging data, not
something a client needs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TrackingEventOut(BaseModel):
    order: str | None = None
    event_type: str | None = None
    location: str | None = None
    vessel: str | None = None
    voyage: str | None = None
    actual_date: str | None = None
    planned_date: str | None = None
    is_actual: bool = False


class TrackingSummaryOut(BaseModel):
    tracking_id: str | None = None
    number: str | None = None
    status: str | None = None
    ops_status: str | None = None
    display_status: str | None = None
    invalid_reason: str | None = None
    found: bool = False
    carrier_code: str | None = None
    carrier_name: str | None = None
    current_location: str | None = None
    eta: str | None = None
    ata: str | None = None
    provider: str | None = None


class TrackingEventsOut(BaseModel):
    number: str | None = None
    found: bool = False
    events: list[TrackingEventOut] = Field(default_factory=list)


class TrackingBulkItemOut(BaseModel):
    number: str | None = None
    found: bool = False
    status: str | None = None
    display_status: str | None = None
    invalid_reason: str | None = None
    duration_seconds: float | None = None


class TrackingBulkResponseOut(BaseModel):
    total: int
    batch_size: int
    total_duration_seconds: float
    success_count: int
    error_count: int
    throughput_per_sec: float
    results: list[TrackingBulkItemOut] = Field(default_factory=list)
