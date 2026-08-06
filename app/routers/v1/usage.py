"""GET /v1/usage - part of the public API surface (API-key authenticated),
so a customer's own integration can check its remaining credits the same
way it checks anything else, without needing a dashboard session.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends

from ...db.session import get_db
from ...dependencies import ApiKeyPrincipal, get_api_key_principal
from ...schemas.common import Page, PageParams
from ...schemas.usage import UsageEventOut, UsageSummary
from ...services.usage_service import UsageService

router = APIRouter(prefix="/v1/usage", tags=["usage"])
_service = UsageService()


@router.get("", response_model=UsageSummary)
def get_usage_summary(principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)):
    balance = _service.get_balance(db, principal.organization.id)
    period_start = datetime.now(timezone.utc) - timedelta(days=30)
    events_count = _service.repo.count_events_since(db, principal.organization.id, period_start)
    return UsageSummary(
        credits_remaining=balance.credits_remaining,
        credits_included_per_period=balance.credits_included_per_period,
        events_this_period=events_count,
    )


@router.get("/events", response_model=Page[UsageEventOut])
def list_usage_events(
    params: PageParams = Depends(), principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)
):
    items, total = _service.list_events(db, principal.organization.id, limit=params.limit, offset=params.offset)
    return Page(items=items, total=total, limit=params.limit, offset=params.offset)
