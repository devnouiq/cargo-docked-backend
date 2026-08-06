from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ContainerCreateRequest(BaseModel):
    container_number: str = Field(min_length=4, max_length=30)
    reference: str | None = None
    carrier_scac: str | None = None


class ContainerBulkCreateRequest(BaseModel):
    container_numbers: list[str] = Field(min_length=1, max_length=500)


class ContainerEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_code: str
    description: str | None
    location: str | None
    vessel: str | None
    voyage: str | None
    occurred_at: datetime | None
    is_actual: bool


class ContainerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    container_number: str
    reference: str | None
    carrier_scac: str | None
    provider_name: str | None
    status: str | None
    last_known_location: str | None
    vessel: str | None
    voyage: str | None
    eta: datetime | None
    etd: datetime | None
    last_free_day: datetime | None
    is_active: bool
    last_polled_at: datetime | None
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
