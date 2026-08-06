"""API key CRUD - JWT-authenticated (dashboard). You can't create your
first API key using an API key, so this is session-only, unlike the
containers/usage/webhooks routes it exists to provision credentials for.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from ...db.session import get_db
from ...dependencies import CurrentSession, get_current_session
from ...schemas.api_key import ApiKeyCreatedResponse, ApiKeyCreateRequest, ApiKeyOut
from ...services.api_key_service import ApiKeyService

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])
_service = ApiKeyService()


@router.post("", response_model=ApiKeyCreatedResponse, status_code=201)
def create_api_key(
    payload: ApiKeyCreateRequest, session: CurrentSession = Depends(get_current_session), db=Depends(get_db)
):
    key, raw_key = _service.create(
        db, organization_id=session.organization.id, created_by_user_id=session.user.id,
        name=payload.name, mode=payload.mode, scopes=payload.scopes,
    )
    return ApiKeyCreatedResponse(**ApiKeyOut.model_validate(key).model_dump(), api_key=raw_key)


@router.get("", response_model=list[ApiKeyOut])
def list_api_keys(session: CurrentSession = Depends(get_current_session), db=Depends(get_db)):
    return _service.list_for_org(db, session.organization.id)


@router.delete("/{key_id}", response_model=ApiKeyOut)
def revoke_api_key(key_id: uuid.UUID, session: CurrentSession = Depends(get_current_session), db=Depends(get_db)):
    return _service.revoke(db, organization_id=session.organization.id, key_id=key_id)


@router.post("/{key_id}/rotate", response_model=ApiKeyCreatedResponse)
def rotate_api_key(key_id: uuid.UUID, session: CurrentSession = Depends(get_current_session), db=Depends(get_db)):
    key, raw_key = _service.rotate(db, organization_id=session.organization.id, key_id=key_id)
    return ApiKeyCreatedResponse(**ApiKeyOut.model_validate(key).model_dump(), api_key=raw_key)
