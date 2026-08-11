"""arq task: scrape one container that the API layer queued.

Enqueued by container_service's `queue_bulk()` (POST /v1/containers/bulk)
and `request_refresh()` (POST /v1/containers/{number}/refresh), which write
the row as `queued` and return immediately rather than holding an HTTP
request open for the whole provider chain - the API runs behind a 300s
Cloud Run request timeout that a large bulk submission would otherwise
risk.

Deliberately charges no credit: the API already charged when the row was
queued.
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)


async def scrape_container(ctx: dict, container_id: str) -> None:
    # Import here, not at module scope: ContainerService pulls in the full
    # provider stack (browser automation deps included) which the arq
    # worker process needs, but keeping it lazy avoids a needless import
    # cost for anything else that imports this module (e.g. tests).
    from ...services.container_service import ContainerService

    service = ContainerService()
    try:
        await service.process_queued_scrape(uuid.UUID(container_id))
    except Exception:  # noqa: BLE001 - the DB row already holds the terminal status
        # Returning cleanly regardless of outcome mirrors deliver_webhook:
        # this task owns its own state in the database, so re-raising would
        # only hand arq a retry decision it has no business making (and
        # a retried scrape re-fires `container.updated` webhooks).
        logger.exception("scrape_container: failed for container %s", container_id)
