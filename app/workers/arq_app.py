"""arq worker process entrypoint: `uv run arq app.workers.arq_app.WorkerSettings`.

Two kinds of jobs share this one worker pool:
  * `deliver_webhook` - enqueued on-demand by services/webhook_service.py
    whenever a tracked container's state changes.
  * `scrape_container` - enqueued on-demand by services/container_service.py
    when a bulk submission or a manual refresh queues a container
    (workers/tasks/scrape.py).

There used to be a third: `refresh_tracked_containers`, a cron job that
re-scraped every active container on a fixed timer and charged a credit
for it. Removed - re-scraping should be something a customer asks for
(directly or via a webhook-driven refresh), not something that silently
drains their credit balance on a clock.
"""

from __future__ import annotations

import logging

from arq import func
from arq.connections import RedisSettings

from ..core.config import settings
from ..core.logging import configure_logging
from .tasks.scrape import scrape_container
from .tasks.webhook_delivery import deliver_webhook

logger = logging.getLogger(__name__)


async def _on_startup(ctx: dict) -> None:
    configure_logging(settings.log_level)
    logger.info("arq worker starting up (env=%s)", settings.env)


async def _on_shutdown(ctx: dict) -> None:
    logger.info("arq worker shutting down")


class WorkerSettings:
    functions = [
        deliver_webhook,
        # max_tries=1 is load-bearing, not tuning: a worker-pool revision
        # swap that SIGTERMs a mid-flight scrape would otherwise get an
        # arq-level retry, re-firing `container.updated` to customer
        # endpoints as a visible duplicate. Status lives in the DB row -
        # there's no background sweep for anything left stuck (the cron
        # poller that used to do that was removed).
        func(scrape_container, name="scrape_container", timeout=settings.scrape_job_timeout_s, max_tries=1),
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    max_jobs = 20
