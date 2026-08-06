"""The standardized `/v1/containers` resource API - the core product.

Replaces the old ad-hoc `/v1/track` naming (kept as deprecated aliases in
routers/tracking.py, not deleted) with proper REST resource routes backed
by the provider registry (providers/registry.py) instead of one hardcoded
scraper.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...core.errors import AppError, NotFoundError
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


@router.post("/bulk", response_model=ContainerBulkResponse)
async def start_tracking_bulk(
    payload: ContainerBulkCreateRequest, principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)
):
    async def _track_one(number: str) -> ContainerBulkResultItem:
        try:
            container = await _service.track(
                db, organization_id=principal.organization.id, api_key_id=principal.api_key.id, container_number=number
            )
            return ContainerBulkResultItem(container_number=number, ok=True, container=container)
        except AppError as exc:
            return ContainerBulkResultItem(container_number=number, ok=False, error=exc.detail)
        except Exception as exc:  # noqa: BLE001 - isolate one item's failure from the rest of the batch
            return ContainerBulkResultItem(container_number=number, ok=False, error=str(exc))

    # Sequential, not gather: every call shares `db` (one SQLAlchemy
    # Session, not safe for concurrent use across coroutines) and hits the
    # same org's credit balance row - concurrent UPDATEs there would just
    # serialize at the DB anyway. Bulk throughput comes from
    # providers/registry.py's own concurrency, not from parallelizing here.
    results = [await _track_one(number) for number in payload.container_numbers]
    return ContainerBulkResponse(results=results)


@router.get("/{number}", response_model=ContainerDetailOut)
async def get_container(
    number: str, principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)
):
    return await _service.get_or_refresh(
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
