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
    #
    # A fresh ContainerService() per job (not a module-level singleton) is
    # deliberate - it's what lets tests monkeypatch a fresh fake provider
    # registry per test (see conftest.py's `_fake_provider_registry`) and
    # have it actually take effect here. The session-pooling benefit
    # (registry.py's SearatesHttpProvider) still applies across jobs in a
    # real worker process regardless - build_default_registry() itself
    # caches and returns the same registry/provider instance every call, so
    # a fresh ContainerService() here still gets the same warmed-up
    # SeaRatesTracker pool as the last job, not an empty one.
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


async def sweep_stuck_scrapes(ctx: dict) -> None:
    """Cron job (registered in workers/arq_app.py): corrects any
    `TrackedContainer` row left `queued`/`in_progress` past
    `settings.scrape_stuck_threshold_s` with no job ever resolving it -
    either the job that owned it crashed/was cancelled (arq's job-timeout
    cancellation not being caught was one confirmed cause, now fixed in
    ContainerService._refresh_and_apply_safely) or it was never actually
    enqueued at all (`_enqueue_scrapes` is best-effort - a Redis hiccup
    leaves a row `queued` with no job behind it).

    Deliberately narrow: this ONLY corrects the row's status to `FAILED`
    with an explanatory message. It never re-enqueues a scrape and never
    charges a credit - unlike the old `refresh_tracked_containers` poller
    (removed - see arq_app.py's module docstring) that re-scraped on a
    timer and silently spent customer credits. A customer whose container
    lands here can freely retry it themselves afterwards (GET/refresh no
    longer treat a `FAILED` row as pending).
    """
    from datetime import datetime, timedelta, timezone

    from ...core.config import settings
    from ...db.session import SessionLocal
    from ...models.container import ContainerScrapeStatus
    from ...repositories.containers import ContainerRepository

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.scrape_stuck_threshold_s)
    repo = ContainerRepository()
    with SessionLocal() as db:
        stuck = repo.find_stuck(db, older_than=cutoff)
        for container in stuck:
            container.tracking_status = ContainerScrapeStatus.FAILED
            container.tracking_message = (
                "Scrape did not complete in time - please try again."
            )
        if stuck:
            db.commit()
            logger.warning("sweep_stuck_scrapes: marked %d stuck container(s) as failed", len(stuck))
