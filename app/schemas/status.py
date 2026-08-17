"""Response shape for `GET /v1/status` (routers/v1/status.py) - the
aggregate, customer-facing status endpoint. Doesn't mirror a model 1:1
like most of this package - it reports on infrastructure health, not a
persisted aggregate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class CheckResult(BaseModel):
    ok: bool


class StatusResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    checks: dict[str, CheckResult]
    checked_at: datetime
