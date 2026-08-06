"""Central config: loads .env once and exposes typed settings.

Replaces the ad-hoc `_load_env_file()` that used to live inside
api_auto_file/tracker.py and the scattered `os.environ.get(...)` calls in
api/main.py - one source of truth for every setting the app reads.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Populate os.environ from the repo-root .env, without overriding
    variables the real environment already set."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./track_trace.db"

    oxylabs_proxy_host: str | None = os.environ.get("OXYLABS_PROXY_HOST")
    oxylabs_proxy_username: str | None = os.environ.get("OXYLABS_PROXY_USERNAME")
    oxylabs_proxy_password: str | None = os.environ.get("OXYLABS_PROXY_PASSWORD")

    scraper_max_pages: int = int(os.environ.get("SCRAPER_MAX_PAGES", "15"))
    scraper_retries: int = int(os.environ.get("SCRAPER_RETRIES", "1"))

    log_level: str = os.environ.get("LOG_LEVEL", "INFO")

    # How long a ContainerResult row is trusted before a route treats it as a
    # cache miss and re-fetches. Matches the interval the SeaRates provider
    # used to apply on its own (now-removed) on-disk cache.
    container_cache_ttl_seconds: int = 6 * 3600


settings = Settings()
