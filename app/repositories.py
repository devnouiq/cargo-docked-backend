"""DB access for cached tracking results - the single cache source of truth
shared by every provider (browser-based and HTTP-based alike), replacing the
on-disk JSON caches the HTTP providers used to keep separately.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .models import ContainerResult


class ContainerResultRepository:
    def get_cached(self, db: Session, container_number: str, ttl_seconds: int) -> Optional[ContainerResult]:
        """Return the cached row if it exists and is still fresh, else None
        (an expired row is treated as a miss - it stays in the table and
        gets overwritten by the next successful `upsert`)."""
        row = db.query(ContainerResult).filter_by(container_number=container_number).one_or_none()
        if row is None:
            return None
        updated_at = row.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - updated_at > timedelta(seconds=ttl_seconds):
            return None
        return row

    def upsert(
        self,
        db: Session,
        container_number: str,
        status: str,
        location: Optional[str],
        raw_data: dict,
    ) -> ContainerResult:
        """Insert or update the row for this container_number.

        Must be an upsert, not a blind insert: `container_number` is unique,
        so re-fetching an already-cached (but expired) number would violate
        that constraint if we always inserted a fresh row.
        """
        row = db.query(ContainerResult).filter_by(container_number=container_number).one_or_none()
        if row is None:
            row = ContainerResult(container_number=container_number)
            db.add(row)
        row.status = status
        row.location = location
        row.raw_data = raw_data
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(row)
        return row
