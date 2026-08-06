"""Shared stealth-browser session lifecycle, used by both browser-based
providers (track_trace_browser, searates_browser) so neither one owns or
reaches into the other's internals.

Every scrape used to launch its own throwaway browser (new Playwright
driver + new Chromium process per container), so N concurrent scrapes meant
N full browsers fighting over the same CPU/RAM - each one got slower the
more ran at once. Instead we keep one shared browser alive and hand out
pooled tabs (up to MAX_CONCURRENT_PAGES) to concurrent scrapes, which is
what actually parallelizes: one browser process, many tabs, real I/O
concurrency.
"""

from __future__ import annotations

import asyncio
import urllib.parse
import uuid
from typing import Optional

from scrapling.fetchers import AsyncStealthySession

from ..config import settings

MAX_CONCURRENT_PAGES = settings.scraper_max_pages

# scrapling's default `retries=3` silently re-runs the whole navigate/fill/
# click/wait-for-popup sequence on any transient error, so one flaky attempt
# can turn into 3x its cost with no visibility. Callers handle failures
# themselves, so disable the hidden retry loop here.
RETRIES = settings.scraper_retries

_session: Optional[AsyncStealthySession] = None
_session_lock = asyncio.Lock()

# Caps how many tabs are open in the shared browser at once, matching
# scrapling's own max_pages so batches larger than this queue here instead
# of inside scrapling's page pool.
dispatch_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)


async def get_shared_session() -> AsyncStealthySession:
    """Return the shared browser session, starting it on first use."""
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                session = AsyncStealthySession(max_pages=MAX_CONCURRENT_PAGES, headless=True, retries=RETRIES)
                await session.start()
                _session = session
    return _session


async def close_scraper_session() -> None:
    """Shut down the shared browser. Call on app shutdown to avoid an orphaned process."""
    global _session
    if _session is not None:
        await _session.close()
        _session = None


def get_proxy_url() -> str:
    """Generate a fresh rotating-session Oxylabs proxy URL on every call.

    Currently unused by either browser provider (disabled - see the TODO in
    track_trace_browser.py) but kept here as the one place proxy-session
    construction lives if/when it's re-enabled.
    """
    session_id = uuid.uuid4().hex[:10]
    username = f"{settings.oxylabs_proxy_username}-cc-US-sessid-{session_id}-sesstime-10"
    encoded_pw = urllib.parse.quote(settings.oxylabs_proxy_password or "", safe="")
    return f"http://{username}:{encoded_pw}@{settings.oxylabs_proxy_host}"
