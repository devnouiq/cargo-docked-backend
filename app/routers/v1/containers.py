"""The standardized `/v1/containers` resource API - the core product.

Replaces the old ad-hoc `/v1/track` naming (kept as deprecated aliases in
routers/tracking.py, not deleted) with proper REST resource routes backed
by the provider registry (providers/registry.py) instead of one hardcoded
scraper.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...core.errors import NotFoundError
from ...db.session import get_db
from ...dependencies import ApiKeyPrincipal, get_api_key_principal
from ...schemas.common import Page, PageParams
from ...schemas.container import (
    ContainerBulkCreateRequest,
    ContainerBulkResponse,
    ContainerBulkResultItem,
    ContainerCreateRequest,
    ContainerDetailOut,
    ContainerEventOut,
    ContainerOut,
)
from ...services.container_service import ContainerService

router = APIRouter(prefix="/v1/containers", tags=["containers"])
_service = ContainerService()


@router.get("", response_model=Page[ContainerOut])
def list_containers(
    params: PageParams = Depends(), principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)
):
    items, total = _service.containers.list_for_org(
        db, principal.organization.id, limit=params.limit, offset=params.offset
    )
    return Page(items=items, total=total, limit=params.limit, offset=params.offset)


@router.post("", response_model=ContainerOut, status_code=201)
async def start_tracking(
    payload: ContainerCreateRequest, principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)
):
    return await _service.track(
        db,
        organization_id=principal.organization.id,
        api_key_id=principal.api_key.id,
        container_number=payload.container_number,
        reference=payload.reference,
        carrier_scac=payload.carrier_scac,
    )


@router.post("/bulk", response_model=ContainerBulkResponse, status_code=202)
async def start_tracking_bulk(
    payload: ContainerBulkCreateRequest, principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)
):
    """Queue a batch of containers for tracking. **Accepted, not completed.**

    This endpoint returns as soon as the containers are registered and
    charged - it does not wait for the carrier data. Each returned container
    comes back with `tracking_status: "queued"` and `status: null`; a
    background worker resolves them shortly afterwards.

    To get results, either poll `GET /v1/containers/{number}` until
    `tracking_status` is terminal (`completed`, `no_data` or `failed`), or
    subscribe to the `container.updated` webhook.

    Duplicate numbers within one payload are collapsed: one row, one credit,
    one job. `queued` counts the containers accepted, `rejected` those
    refused up front (e.g. insufficient credits) - a rejected item has
    `ok: false` and an `error`, and is never scraped.
    """
    results = await _service.queue_bulk(
        db,
        organization_id=principal.organization.id,
        api_key_id=principal.api_key.id,
        container_numbers=payload.container_numbers,
    )
    items = [
        ContainerBulkResultItem(container_number=number, ok=container is not None, container=container, error=error)
        for number, container, error in results
    ]
    return ContainerBulkResponse(
        results=items,
        queued=sum(1 for item in items if item.ok),
        rejected=sum(1 for item in items if not item.ok),
    )


@router.get("/{number}", response_model=ContainerDetailOut)
async def get_container(
    number: str, principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)
):
    return await _service.get_or_refresh(
        db, organization_id=principal.organization.id, api_key_id=principal.api_key.id, container_number=number
    )


@router.post("/{number}/refresh", response_model=ContainerOut, status_code=202)
async def refresh_container(
    number: str, principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)
):
    """Queue a fresh scrape of an already-tracked container.

    Returns immediately with `tracking_status: "queued"`; the worker does the
    actual lookup. Charges one credit - except when a scrape is already
    pending (`queued`/`in_progress`), in which case the existing container
    is returned unchanged and nothing is charged or re-queued.
    """
    return await _service.request_refresh(
        db, organization_id=principal.organization.id, api_key_id=principal.api_key.id, container_number=number
    )


@router.get("/{number}/events", response_model=list[ContainerEventOut])
def get_container_events(number: str, principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)):
    container = _service.containers.get_with_events(db, organization_id=principal.organization.id, container_number=number)
    if container is None:
        raise NotFoundError(f"Container {number!r} is not being tracked for this organization.")
    return container.events


@router.delete("/{number}", status_code=204)
async def stop_tracking(number: str, principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)) -> None:
    stopped = await _service.stop_tracking(db, organization_id=principal.organization.id, container_number=number)
    if not stopped:
        raise NotFoundError(f"Container {number!r} is not being tracked for this organization.")
