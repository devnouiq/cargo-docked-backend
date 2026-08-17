"""Deprecated aliases for the pre-rework `/v1/track*` paths.

Superseded by the standardized `/v1/containers` resource routes
(routers/v1/containers.py), but kept mounted and working - not removed -
per the "standardize the naming, don't break existing integrations"
brief. New integrations should use `/v1/containers`; these are marked
`deprecated=True` so they still show up (crossed out) in the generated
API docs instead of disappearing silently.
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..core.errors import AppError, NotFoundError
from ..db.session import get_db
from ..dependencies import ApiKeyPrincipal, get_api_key_principal
from ..schemas import BulkTrackRequest, TrackRequest, TrackResponse
from ..services.container_service import ContainerService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["tracking (deprecated)"])
_service = ContainerService()


def _to_track_response(container) -> TrackResponse:
    return TrackResponse(
        container_number=container.container_number,
        status=container.status or "unknown",
        location=container.last_known_location,
        raw_data=container.raw_data,
        cached=False,
        duration_seconds=None,
    )


@router.post("/track", response_model=List[TrackResponse], deprecated=True)
async def track_containers(
    request: TrackRequest, principal: ApiKeyPrincipal = Depends(get_api_key_principal), db: Session = Depends(get_db)
):
    """Deprecated: use `POST /v1/containers/bulk`."""
    results = []
    for number in request.container_numbers:
        try:
            container = await _service.track(
                db, organization_id=principal.organization.id, api_key_id=principal.api_key.id, container_number=number
            )
            results.append(_to_track_response(container))
        except AppError as exc:
            results.append(TrackResponse(container_number=number, status="error", location=None, raw_data={"error": exc.detail}))
    return results


@router.get("/track/{number}", response_model=TrackResponse, deprecated=True)
async def track_single(number: str, principal: ApiKeyPrincipal = Depends(get_api_key_principal), db: Session = Depends(get_db)):
    """Deprecated: use `GET /v1/containers/{number}`. Unlike the new route,
    this one auto-registers tracking on first call (the old `/v1/track`
    had no separate "start tracking" step - every number was trackable via
    a single GET)."""
    try:
        container = await _service.get_or_refresh(
            db, organization_id=principal.organization.id, api_key_id=principal.api_key.id, container_number=number
        )
    except NotFoundError:
        container = await _service.track(
            db, organization_id=principal.organization.id, api_key_id=principal.api_key.id, container_number=number
        )
    return _to_track_response(container)


@router.get("/providers", deprecated=True)
async def get_providers(principal: ApiKeyPrincipal = Depends(get_api_key_principal)):
    """Deprecated, informational only - the provider registry
    (providers/registry.py) now decides per-container which source to use.

    Deliberately returns an empty list: this used to expose the internal
    provider-registry identifiers (e.g. "searates_http"), which is the same
    category of customer-facing internal-detail leak as the old
    `scrape_status`/`provider_name` fields - kept mounted (not removed, per
    the deprecate-don't-break convention) but emptied out."""
    return {"providers": []}
