"""arq cron task: periodically re-checks every actively-tracked container
through the provider registry and lets container_service emit webhook
events for whatever changed - the mechanism behind the whole "carrier data
engine" promise (containers update themselves in the background, not only
when a customer happens to poll `GET /v1/containers/{number}`).
"""

from __future__ import annotations

import logging

from ...core.config import settings
from ...db.session import SessionLocal
from ...repositories.containers import ContainerRepository

logger = logging.getLogger(__name__)
_container_repo = ContainerRepository()


async def refresh_tracked_containers(ctx: dict) -> None:
    # Import here, not at module scope: ContainerService pulls in the full
    # provider stack (browser automation deps included) which the arq
    # worker process needs, but keeping it lazy avoids a needless import
    # cost for anything else that imports this module (e.g. tests).
    from ...services.container_service import ContainerService

    with SessionLocal() as db:
        due = _container_repo.list_active_for_polling(db, limit=settings.poller_batch_size)
        container_ids = [c.id for c in due]

    if not container_ids:
        return

    service = ContainerService()
    logger.info("poller: refreshing %d tracked containers", len(container_ids))
    for container_id in container_ids:
        try:
            await service.refresh_and_notify(container_id)
        except Exception:  # noqa: BLE001 - one container's failure must not stop the sweep
            logger.exception("poller: failed to refresh container %s", container_id)
