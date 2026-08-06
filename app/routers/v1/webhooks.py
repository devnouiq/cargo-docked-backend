"""Webhook endpoint CRUD + delivery history - API-key authenticated, same
as containers/usage (see routers/v1/__init__.py docstring)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from ...db.session import get_db
from ...dependencies import ApiKeyPrincipal, get_api_key_principal
from ...schemas.webhook import WebhookCreatedResponse, WebhookCreateRequest, WebhookDeliveryOut, WebhookOut, WebhookUpdateRequest
from ...services.webhook_service import WebhookService

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])
_service = WebhookService()


@router.post("", response_model=WebhookCreatedResponse, status_code=201)
def create_webhook(payload: WebhookCreateRequest, principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)):
    endpoint = _service.create(
        db,
        organization_id=principal.organization.id,
        url=str(payload.url),
        description=payload.description,
        event_types=[e.value for e in payload.event_types],
    )
    return WebhookCreatedResponse(**WebhookOut.model_validate(endpoint).model_dump(), secret=endpoint.secret)


@router.get("", response_model=list[WebhookOut])
def list_webhooks(principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)):
    return _service.list_for_org(db, principal.organization.id)


@router.patch("/{webhook_id}", response_model=WebhookOut)
def update_webhook(
    webhook_id: uuid.UUID, payload: WebhookUpdateRequest, principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)
):
    fields = payload.model_dump(exclude_unset=True)
    if "url" in fields and fields["url"] is not None:
        fields["url"] = str(fields["url"])
    if "event_types" in fields and fields["event_types"] is not None:
        fields["event_types"] = [e.value if hasattr(e, "value") else e for e in fields["event_types"]]
    return _service.update(db, organization_id=principal.organization.id, endpoint_id=webhook_id, **fields)


@router.delete("/{webhook_id}", status_code=204)
def delete_webhook(webhook_id: uuid.UUID, principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)) -> None:
    _service.delete(db, organization_id=principal.organization.id, endpoint_id=webhook_id)


@router.get("/{webhook_id}/deliveries", response_model=list[WebhookDeliveryOut])
def list_deliveries(webhook_id: uuid.UUID, principal: ApiKeyPrincipal = Depends(get_api_key_principal), db=Depends(get_db)):
    return _service.list_deliveries(db, organization_id=principal.organization.id, endpoint_id=webhook_id)
