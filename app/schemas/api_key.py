from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ..models.api_key import ApiKeyMode


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    mode: ApiKeyMode = ApiKeyMode.SANDBOX
    scopes: list[str] = Field(default_factory=list)


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    mode: ApiKeyMode
    key_prefix: str
    scopes: list[str]
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreatedResponse(ApiKeyOut):
    """Returned only at creation/rotation time - the only moment the raw
    key is ever available; it is not recoverable afterwards."""

    api_key: str
