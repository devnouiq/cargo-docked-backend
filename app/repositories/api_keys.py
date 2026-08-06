from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models.api_key import ApiKey, ApiKeyMode


class ApiKeyRepository:
    def create(
        self,
        db: Session,
        *,
        organization_id: uuid.UUID,
        created_by_user_id: uuid.UUID | None,
        name: str,
        mode: ApiKeyMode,
        key_prefix: str,
        key_hash: str,
        scopes: list[str],
    ) -> ApiKey:
        key = ApiKey(
            organization_id=organization_id,
            created_by_user_id=created_by_user_id,
            name=name,
            mode=mode,
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=scopes,
        )
        db.add(key)
        db.flush()
        return key

    def get_by_hash(self, db: Session, key_hash: str) -> ApiKey | None:
        return db.query(ApiKey).filter_by(key_hash=key_hash).one_or_none()

    def get(self, db: Session, key_id: uuid.UUID, *, organization_id: uuid.UUID) -> ApiKey | None:
        return db.query(ApiKey).filter_by(id=key_id, organization_id=organization_id).one_or_none()

    def list_for_org(self, db: Session, organization_id: uuid.UUID) -> list[ApiKey]:
        return (
            db.query(ApiKey)
            .filter_by(organization_id=organization_id)
            .order_by(ApiKey.created_at.desc())
            .all()
        )

    def revoke(self, db: Session, key: ApiKey) -> ApiKey:
        key.revoked_at = datetime.now(timezone.utc)
        db.flush()
        return key

    def touch_last_used(self, db: Session, key: ApiKey) -> None:
        key.last_used_at = datetime.now(timezone.utc)
        db.flush()
