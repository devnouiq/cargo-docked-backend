from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from ..core.errors import NotFoundError
from ..core.security import GeneratedApiKey, generate_api_key
from ..models.api_key import ApiKey, ApiKeyMode
from ..repositories.api_keys import ApiKeyRepository


class ApiKeyService:
    def __init__(self) -> None:
        self.repo = ApiKeyRepository()

    def create(
        self,
        db: Session,
        *,
        organization_id: uuid.UUID,
        created_by_user_id: uuid.UUID,
        name: str,
        mode: ApiKeyMode,
        scopes: list[str],
    ) -> tuple[ApiKey, str]:
        generated: GeneratedApiKey = generate_api_key(live=mode is ApiKeyMode.LIVE)
        key = self.repo.create(
            db,
            organization_id=organization_id,
            created_by_user_id=created_by_user_id,
            name=name,
            mode=mode,
            key_prefix=generated.raw_key[: len(generated.prefix) + 6],
            key_hash=generated.key_hash,
            scopes=scopes,
        )
        db.commit()
        db.refresh(key)
        return key, generated.raw_key

    def list_for_org(self, db: Session, organization_id: uuid.UUID) -> list[ApiKey]:
        return self.repo.list_for_org(db, organization_id)

    def revoke(self, db: Session, *, organization_id: uuid.UUID, key_id: uuid.UUID) -> ApiKey:
        key = self.repo.get(db, key_id, organization_id=organization_id)
        if key is None:
            raise NotFoundError("API key not found.")
        key = self.repo.revoke(db, key)
        db.commit()
        db.refresh(key)
        return key

    def rotate(self, db: Session, *, organization_id: uuid.UUID, key_id: uuid.UUID) -> tuple[ApiKey, str]:
        """Revoke the old key and mint a replacement with the same
        name/mode/scopes, so callers rotate compromised keys without losing
        their configured scope."""
        old_key = self.repo.get(db, key_id, organization_id=organization_id)
        if old_key is None:
            raise NotFoundError("API key not found.")
        self.repo.revoke(db, old_key)
        new_key, raw_key = self.create(
            db,
            organization_id=organization_id,
            created_by_user_id=old_key.created_by_user_id,
            name=old_key.name,
            mode=old_key.mode,
            scopes=old_key.scopes,
        )
        return new_key, raw_key
