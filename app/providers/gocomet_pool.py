"""Pre-warmed pool of GoCometTracker instances - ports load_test_gocomet.py's
`SessionPool` pattern into the two live debug routes that actually see
concurrent frontend traffic (routers/searates_debug.py's single-lookup and
bulk routes; NOT the standardized /v1/containers path, which already reuses
one tracker per worker thread via registry.py's GoCometHttpProvider).

Why this exists: a live 50-container load test against these two routes
(no pooling at the time) showed real latency/connection strain under
concurrency - each caller/worker built a brand-new curl_cffi session and
proxy connection cold, right when it needed one, so a burst of concurrent
lookups meant a burst of simultaneous fresh Oxylabs handshakes. A pool of
already-built, already-connected (confirmed via a free IP-echo call)
trackers sitting ready means acquiring one is a queue-pop, not a proxy
handshake - the same fix load_test_gocomet.py's own docstring describes:
"That swap is instant, not 'wait then build one'".

Unlike the load-test script (a one-shot CLI that builds a pool once and
exits), this is a long-lived FastAPI process - the pool is a lazily-built
module-level singleton, prefilled once on first use and kept topped up by
background rebuilds thereafter (discard() kicks one off, doesn't block the
caller on it).
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import threading

from .gocomet_http import GoCometTracker, TrackerConfig

logger = logging.getLogger(__name__)

# Free, doesn't touch GoComet - only warms up a pooled tracker's session
# (opens its proxy connection, locks in an exit IP) before it's ever needed
# for a real lookup. Same endpoint load_test_gocomet.py and searates_http.py
# both already use for this exact purpose.
IP_ECHO_URL = "https://api.ipify.org?format=json"

# Modest default: these two debug routes share Oxylabs' account-wide
# concurrent-connection budget with the standardized /v1/containers path's
# own reserved slices (see main.py/workers/arq_app.py) - not sized to cover
# every possible concurrent caller, just enough that a normal burst doesn't
# all cold-start at once. acquire() falls back to a synchronous fresh build
# if the pool is ever fully drained, so correctness never depends on this
# number being "enough" - it's a latency optimization only.
_DEFAULT_POOL_SIZE = 8
_DEFAULT_BUILD_WORKERS = 8
_ACQUIRE_TIMEOUT_S = 5.0


class GoCometSessionPool:
    def __init__(self, tracker_kwargs: dict, target_size: int = _DEFAULT_POOL_SIZE, build_workers: int = _DEFAULT_BUILD_WORKERS):
        self._tracker_kwargs = tracker_kwargs
        self.target_size = target_size
        self._queue: "queue.Queue[GoCometTracker]" = queue.Queue()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, build_workers))

    def _build_one(self) -> None:
        tracker = GoCometTracker(TrackerConfig(**self._tracker_kwargs))
        try:
            tracker._session.get(IP_ECHO_URL, timeout=10.0)
        except Exception:  # noqa: BLE001 - still usable even if the warm-up ping itself failed
            pass
        self._queue.put(tracker)

    def prefill(self) -> None:
        """Build and warm `target_size` trackers in parallel, BLOCKING until
        ready. Only safe to call from somewhere already off the asyncio
        event loop (e.g. app startup before the loop is serving traffic, or
        a call already wrapped in asyncio.to_thread) - `get_pool()` below
        deliberately does NOT call this, since it runs directly on a route
        handler's event-loop thread."""
        futures = [self._executor.submit(self._build_one) for _ in range(self.target_size)]
        concurrent.futures.wait(futures)
        logger.info("GoComet session pool prefilled: %d trackers ready", self.target_size)

    def start_filling(self) -> None:
        """Non-blocking: submits `target_size` background builds and
        returns immediately - callers before the pool is actually warm just
        fall through to acquire()'s synchronous-build fallback (no worse
        than having no pool at all), rather than the event loop stalling on
        a blocking prefill the first time a request happens to trigger it."""
        for _ in range(self.target_size):
            self._executor.submit(self._build_one)

    def acquire(self) -> GoCometTracker:
        """Instant if a pre-warmed tracker is queued. If the pool is
        currently drained (a burst bigger than target_size, or still
        rebuilding after several discards), waits briefly, then falls back
        to building one synchronously rather than blocking the caller
        indefinitely - correctness never depends on the pool being big
        enough, only latency does."""
        try:
            return self._queue.get(timeout=_ACQUIRE_TIMEOUT_S)
        except queue.Empty:
            logger.warning("GoComet session pool drained, building a tracker synchronously")
            return GoCometTracker(TrackerConfig(**self._tracker_kwargs))

    def release(self, tracker: GoCometTracker) -> None:
        """A still-good tracker goes straight back for reuse - most exit
        IPs are good for more than one lookup before they get flagged."""
        self._queue.put(tracker)

    def discard(self, tracker: GoCometTracker) -> None:
        """A burned tracker (rate-limited/quota-exceeded, or otherwise
        proven bad) is dropped; a background rebuild replaces it without
        blocking whoever called discard()."""
        try:
            tracker._session.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup only
            pass
        self._executor.submit(self._build_one)

    def pending_spares(self) -> int:
        return self._queue.qsize()


_pool: GoCometSessionPool | None = None
_pool_lock = threading.Lock()


def get_pool(tracker_kwargs: dict) -> GoCometSessionPool:
    """Lazily creates the module-level singleton pool on first call, from
    whichever caller happens to need it first (the single or the bulk route
    - both share one pool, same as they'd share Oxylabs' connection budget
    either way). Later calls reuse it regardless of `tracker_kwargs` passed
    (proxy credentials don't change at runtime).

    Deliberately non-blocking (`start_filling()`, not `prefill()`): this
    runs directly on a route handler's event-loop thread (not inside
    asyncio.to_thread), so a blocking prefill here would stall every other
    request on this process the first time either route happened to trigger
    lazy init. acquire()'s synchronous-build fallback covers callers that
    arrive before the background fill catches up - no worse than having no
    pool at all until then.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:  # re-check inside the lock - another thread may have won the race
                pool = GoCometSessionPool(tracker_kwargs)
                pool.start_filling()
                _pool = pool
    return _pool
