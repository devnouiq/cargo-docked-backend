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

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from arq import func
from arq.connections import RedisSettings

from ..core.config import settings
from ..core.logging import configure_logging
from ..providers.registry import build_default_registry
from .tasks.scrape import scrape_container
from .tasks.webhook_delivery import deliver_webhook

logger = logging.getLogger(__name__)

# This process's reserved slice of Oxylabs' account-wide concurrent-
# connection limit (~18-20 total, measured live). The API process
# (app/main.py, its own slice: 6) and this worker process each get a
# fixed, non-overlapping budget - see
# SearatesHttpProvider.configure_concurrency for why a static per-process
# split (not a shared/coordinated limiter) is what actually guarantees a
# customer's single lookup is never queued behind bulk background
# scraping: they're different processes spending from different budgets,
# never competing for the same pool. 6 + 12 = 18, safely under the
# observed ceiling with margin.
_MAX_CONCURRENT_SEARATES_CONNECTIONS = 12

# Worth warming close to this process's full connection budget, so a bulk
# batch arriving right after startup doesn't cold-start most of its own
# concurrency. Fire-and-forget (see _on_startup) - never delays the worker
# picking up its first real job. Capped at the budget above - warming goes
# through the same semaphore as real jobs, so warming more than the budget
# would just queue instead of actually running any faster.
_WARM_POOL_SIZE = _MAX_CONCURRENT_SEARATES_CONNECTIONS

# Every scrape_container job's actual network work runs via asyncio.to_thread
# (registry.py's SearatesHttpProvider), which - unlike max_jobs below - is
# NOT sized by arq at all. It shares the event loop's *default* executor,
# which Python sizes to min(32, cpu_count()+4) threads by default - on a
# 16-core box that's 20, well under max_jobs=50. Jobs beyond that don't run
# concurrently, they queue silently behind whichever 20 got a thread first -
# confirmed live: a 50-job batch that should finish in one wave visibly split
# into two clusters ~30s apart (one attempt-timeout's width) instead. A
# hedged lookup (registry.py) can also occupy two threads at once, so this
# is sized well above max_jobs, not 1:1 with it.
_THREAD_POOL_SIZE = 120


async def _on_startup(ctx: dict) -> None:
    configure_logging(settings.log_level)
    logger.info("arq worker starting up (env=%s)", settings.env)
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=_THREAD_POOL_SIZE, thread_name_prefix="scrape-worker")
    )
    registry = build_default_registry()
    registry.configure_concurrency(_MAX_CONCURRENT_SEARATES_CONNECTIONS)
    ctx["warm_task"] = asyncio.create_task(registry.warm(_WARM_POOL_SIZE))


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
    # Deliberately conservative, and not the lever for bulk throughput -
    # raising this doesn't help past a certain point. Live testing (50-item
    # batches, repeated with the DB pool and thread pool both raised well
    # past this number to rule them out as the cause) consistently showed
    # ~20 jobs succeed immediately, then the rest stall for exactly one
    # curl connect-timeout (searates_http.py's request_timeout_s=30s)
    # before succeeding in a second wave - the signature of hitting
    # Oxylabs' own concurrent-connection cap on this account, not anything
    # tunable on our side. Past that ceiling, every extra "concurrent" job
    # just means more requests stacking up waiting on connections the
    # proxy won't open yet, each eating a full 30s timeout before its
    # retry can succeed - slower overall than not exceeding the ceiling in
    # the first place. Set below the observed ceiling so jobs succeed
    # cleanly on their first attempt instead.
    max_jobs = 18
