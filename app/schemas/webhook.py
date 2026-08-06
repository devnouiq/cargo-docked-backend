from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from ..models.webhook import WebhookDeliveryStatus, WebhookEventType


class WebhookCreateRequest(BaseModel):
    url: HttpUrl
    description: str | None = None
    event_types: list[WebhookEventType] = Field(min_length=1)


class WebhookUpdateRequest(BaseModel):
    url: HttpUrl | None = None
    description: str | None = None
    event_types: list[WebhookEventType] | None = None
    is_active: bool | None = None


class WebhookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    description: str | None
    event_types: list[str]
    is_active: bool
    created_at: datetime


class WebhookCreatedResponse(WebhookOut):
    """Secret is only ever returned at creation time (like an API key) -
    verify webhook signatures with it and store it client-side then."""

    secret: str


class WebhookDeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: WebhookEventType
    status: WebhookDeliveryStatus
    attempt_count: int
    last_attempted_at: datetime | None
    next_attempt_at: datetime | None
    response_status_code: int | None
    created_at: datetime
