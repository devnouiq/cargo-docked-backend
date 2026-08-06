"""arq worker process entrypoint: `uv run arq app.workers.arq_app.WorkerSettings`.

Two kinds of jobs share this one worker pool:
  * `deliver_webhook` - enqueued on-demand by services/webhook_service.py
    whenever a tracked container's state changes.
  * `refresh_tracked_containers` - run on a fixed schedule via arq's
    `cron_jobs`, the carrier-refresh poller (workers/tasks/poller.py).
"""

from __future__ import annotations

import logging

from arq import cron
from arq.connections import RedisSettings

from ..core.config import settings
from ..core.logging import configure_logging
from .tasks.poller import refresh_tracked_containers
from .tasks.webhook_delivery import deliver_webhook

logger = logging.getLogger(__name__)


async def _on_startup(ctx: dict) -> None:
    configure_logging(settings.log_level)
    logger.info("arq worker starting up (env=%s)", settings.env)


async def _on_shutdown(ctx: dict) -> None:
    logger.info("arq worker shutting down")


class WorkerSettings:
    functions = [deliver_webhook, refresh_tracked_containers]
    cron_jobs = [cron(refresh_tracked_containers, minute=set(range(0, 60, settings.poller_refresh_interval_minutes)))]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    max_jobs = 20
