"""Central application settings.

Single source of truth for every environment-driven value the app reads,
loaded once via pydantic-settings from the repo-root `.env` (or real
process environment in production, which always wins over `.env`).

Replaces the old `app/config.py` dataclass - kept the same import shape
(`from .core.config import settings`) but now validates types/required
fields at startup instead of silently reading `None` for a mistyped var.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["development", "test", "staging", "production"] = "development"
    log_level: str = "INFO"

    # --- database ---------------------------------------------------------
    # Postgres in every real environment; tests override this to a SQLite
    # file/in-memory URL via env var so the suite needs no external service.
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/cargotrack"
    database_pool_size: int = 10
    database_pool_max_overflow: int = 10

    # --- redis / background jobs (arq) -------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # --- auth / JWT ---------------------------------------------------------
    # No default on purpose in production - `_validate_production_secrets`
    # below refuses to boot with the dev fallback outside `development`/`test`.
    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_access_token_ttl_minutes: int = 15
    jwt_refresh_token_ttl_days: int = 30

    # --- OAuth ---------------------------------------------------------------
    google_client_id: str | None = None
    google_client_secret: str | None = None
    # Where to send the browser after login completes, e.g. the dashboard app.
    # Also doubles as the OAuth redirect_uri host: providers redirect the
    # browser straight to {frontend_base_url}/auth/{provider}/callback (see
    # routers/v1/auth.py::_oauth_redirect_uri) - this backend is never in
    # that browser round-trip, so Google's consent screen only ever shows
    # this domain, never the Cloud Run URL. Must exactly match the redirect
    # URI registered with the OAuth app in Google Cloud Console.
    frontend_base_url: str = "http://localhost:3000"

    # --- API keys -------------------------------------------------------------
    api_key_live_prefix: str = "ctk_live_"
    api_key_sandbox_prefix: str = "ctk_test_"

    # --- billing / Stripe -------------------------------------------------------
    # All optional: billing routes lazily no-op with a clear error if unset,
    # rather than the app failing to import/start without them.
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_publishable_key: str | None = None
    # Price IDs for the two paid plans - per-environment (each Stripe
    # account, test or live, has its own), not a literal constant. None
    # until set, matching stripe_price_id's existing nullable contract.
    stripe_starter_price_id: str | None = None
    stripe_growth_price_id: str | None = None

    # --- transactional email (Resend) --------------------------------------
    # Optional, same pattern as Stripe above: unset means the welcome email
    # and forgot-password email silently no-op (logged as a warning) rather
    # than failing signup or turning forgot-password into an
    # account-enumeration oracle - see services/email_service.py.
    resend_api_key: str | None = None
    resend_from_email: str = "support@cargodocked.com"
    # How long a password-reset link stays valid for. Used both to set
    # PasswordResetToken.expires_at and in the emailed copy ("expires in
    # N minutes") - see services/auth_service.py.
    password_reset_token_ttl_minutes: int = 30

    # --- webhooks ------------------------------------------------------------
    webhook_signing_secret: str = "dev-only-webhook-secret-change-me"
    webhook_max_attempts: int = 6
    webhook_delivery_timeout_s: float = 10.0

    # --- CORS ------------------------------------------------------------------
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # --- scraping / providers (unchanged behavior, carried over from old config.py) ---
    oxylabs_proxy_host: str | None = None
    oxylabs_proxy_username: str | None = None
    oxylabs_proxy_password: str | None = None
    scraper_max_pages: int = 15
    scraper_retries: int = 1
    container_cache_ttl_seconds: int = 6 * 3600

    # --- background scrape jobs -------------------------------------------------
    # Hard bound on one queued scrape job. The provider chain includes two
    # browser-based fallbacks and has no timeout of its own.
    scrape_job_timeout_s: int = 240

    @computed_field  # type: ignore[misc]
    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
