from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ..models.usage import UsageEventType


class UsageSummary(BaseModel):
    credits_remaining: int
    credits_included_per_period: int
    events_this_period: int


class UsageEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: UsageEventType
    credits_charged: int
    container_number: str | None
    created_at: datetime
