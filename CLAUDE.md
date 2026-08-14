# CLAUDE.md

Guidance for Claude Code (or any AI agent) working in this repository.

## What this is

CargoTrack: a production container-tracking SaaS API (FastAPI + Python 3.12).
Backend only - the dashboard/marketing frontend is a separate repo. This
repo owns:

- The public `/v1` tracking API (containers, bulk lookup, events, webhooks, usage)
- Auth (OAuth + JWT sessions), org/team management, API keys
- Usage metering / credits, Stripe billing
- The carrier/terminal data engine: a provider-registry adapter over
  several scrapers. Re-scraping a tracked container is customer-initiated
  (manual refresh, bulk import, or a fresh lookup) - there is no background
  poller re-scraping containers on a timer (removed: it silently spent
  customer credits on a fixed schedule).

## Architecture map

```
app/
  core/          settings, JWT/password/API-key/webhook crypto, error types, logging, middleware
  db/             SQLAlchemy Base + engine/session (Postgres in prod, SQLite in tests)
  models/         one file per aggregate (organization, user, auth, api_key, usage, container, webhook, billing)
  schemas/        Pydantic request/response models, mirrors models/ 1:1
  repositories/   DB access only - no business logic, no HTTP concerns
  services/       business logic/orchestration - the layer routers call into
  providers/      carrier/terminal data sources + the registry that unifies them
  routers/v1/     the standardized /v1 API (JWT-auth dashboard routes + API-key-auth product routes)
  routers/        tracking.py (deprecated /v1/track* aliases), searates_debug.py (internal, untouched)
  workers/        arq worker process: webhook delivery + on-demand container scraping
alembic/          schema migrations - the only thing that creates/changes tables
tests/            pytest suite (SQLite, hermetic - no real DB/Redis/network needed)
```

Request flow for the product API: `router → service → repository → model`.
Routers do no direct DB queries or business rules; services own
transactions and call repositories for persistence.

## The two auth modes - do not blur these

- **JWT Bearer** (`Authorization: Bearer <access_token>`, via
  `dependencies.get_current_session`): the dashboard's own account -
  signup/login/OAuth, org/member management, API-key CRUD, billing. You
  can't mint your first API key using an API key, so key-provisioning
  routes are JWT-only.
- **API key** (`X-API-Key` header, via `dependencies.get_api_key_principal`):
  the actual product surface - `/v1/containers`, `/v1/usage`,
  `/v1/webhooks`. This is what customer integrations/SDKs use.

If you add a new `/v1` route, decide which bucket it belongs to based on
this split, not by copying whichever dependency happens to be nearby.

## The provider registry (the "carrier data engine")

`app/providers/registry.py` is the adapter-pattern seam mentioned in the
original product brief. Today it wraps four existing scrapers behind one
`TrackingProvider` interface (`app/providers/base.py`):

| Provider | File | Notes |
|---|---|---|
| `searates_http` | `providers/searates_http.py` | **Do not modify.** Polite, TLS-impersonated HTTP client - primary source. Load-tested rate-limit/proxy-rotation logic lives here. |
| `romeu_http` | `providers/romeu_http.py` | Romeu Shipping's own API, only claims ROMU-prefixed numbers. |
| `track_trace_browser` | `providers/track_trace_browser.py` | Broad carrier coverage via a real headless browser. |
| `searates_browser` | `providers/searates_browser.py` | Browser-based SeaRates fallback/diagnostic. |

`ProviderRegistry.track()` tries each provider in order (cheapest/most
reliable first) until one returns `ok=True`. **To add a real data
aggregator later (Terminal49/Vizion/project44):** write one more adapter
class with an async `track(number) -> NormalizedTrackingResult` method and
a `supports(number) -> bool`, append it to `build_default_registry()`.
Nothing else in the app needs to change - services/routers only ever call
the registry, never a specific scraper.

## Files that must not change behavior

- **`app/providers/searates_http.py`** - production-quality, load-tested
  rate-limit/proxy-rotation logic. Wrap it (see `registry.py`), don't edit it.
- **`app/routers/searates_debug.py`** - internal debug router
  (`/v1/track-searates*`, `/v1/track-searates-browser/*`). Kept mounted
  and working exactly as before; new product routes live in
  `routers/v1/containers.py` instead. If you need to touch the shared
  `ContainerResult` cache table it reads/writes, go through
  `app/repositories/container_cache.py` (the same repository it already
  uses), not a new path.
- **`app/database.py`, `app/config.py`, `app/schemas/legacy.py`** -
  backward-compatible shims kept so the above two files' imports
  (`from .database import ...`, `from .config import settings`, `from
  .schemas import BulkTrackRequest`) resolve unchanged, now pointed at the
  real `db/`, `core/config.py`, `schemas/` implementations. Don't
  "clean these up" by inlining them back into the untouched files - that's
  what would actually change those files' behavior/coupling.

## Database & migrations

Postgres in every real environment (`DATABASE_URL`), SQLite for tests
(`tests/conftest.py` sets this before any `app.*` import happens - order
matters, `app/core/config.py` reads the env once at import time via
`pydantic-settings`).

**Alembic owns the schema.** Nothing in the app calls
`Base.metadata.create_all()` outside of tests. To change a model:

1. Edit the model in `app/models/`.
2. Write a migration by hand in `alembic/versions/` (the existing
   `192b867b97bf_initial_schema.py` was hand-written for the same reason:
   `alembic revision --autogenerate` needs a live DB to diff against,
   which isn't always available - don't assume it works without checking).
   Match the naming convention in `app/db/base.py` (`pk_*`, `fk_*_*_*`,
   `uq_*_*`, `ix_*_*`) so future autogenerate diffs stay clean.
3. `uv run alembic upgrade head` against a real (or throwaway SQLite -
   `DATABASE_URL=sqlite:///./scratch.db uv run alembic upgrade head`)
   database to confirm it actually applies before committing.

## Local development

```bash
uv sync
cp .env.example .env   # fill in OXYLABS_* at minimum for live provider calls
docker compose up -d postgres redis
uv run alembic upgrade head
uv run python scripts/init_db.py   # seeds plans + a dev org/user/API key
uv run uvicorn app.main:app --reload
# separately, for webhook delivery + background container scraping:
uv run arq app.workers.arq_app.WorkerSettings
```

OAuth login and Stripe billing degrade gracefully with no credentials set
(`FeatureNotConfiguredError` → HTTP 503 on just those routes) - everything
else works. See `.env.example` for what each variable gates.

## Testing

```bash
uv run python -m pytest tests/ -v

# To run the identical suite against real Postgres instead of SQLite
# (confirmed to pass - see git history) - point it at a *disposable*
# database, never one the app itself is using:
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/cargotrack_test \
  uv run python -m pytest tests/ -v
```

64 tests, hermetic by default: SQLite, no real Postgres/Redis/network
required. `tests/conftest.py` recreates the schema per test
(`Base.metadata.create_all`/`drop_all`) and monkeypatches the
container-tracking provider registry to a canned fake
(`_fake_provider_registry`) so nothing hits SeaRates/Romeu/track-trace.com.
`test_api.py`, `test_browser_headers_comparison.py`, `test_own_ip.py`
predate this suite and are manual smoke/benchmark scripts against a
*running* server, not pytest tests - excluded from collection in
`pyproject.toml`'s `addopts`, run them by hand if needed.

Coverage by area: `test_security.py` (password/JWT/API-key/webhook crypto,
no DB), `test_provider_registry.py` (carrier-response parsing + provider
fallback ordering, no network - the actual "hard part" logic), `test_auth.py`
+ `test_organizations.py` (signup/login/OAuth-adjacent flows, membership/
role enforcement), `test_api_keys.py`, `test_usage.py` (credit metering +
quota enforcement), `test_containers.py` (the product API end-to-end),
`test_webhooks.py` (CRUD) + `test_webhook_delivery_worker.py` (the arq
delivery task itself - signing, retry/backoff, exhaustion - httpx and the
arq pool both faked), `test_billing.py` (plan/subscription reads +
graceful 503 with no Stripe keys set).

When adding a subsystem, add tests the same way: hit the HTTP layer via
`client` (a `TestClient`), not the service layer directly, so route wiring
(auth mode, status codes, response shape) is covered too.

## Conventions

- **SOLID/DRY, no speculative abstraction.** Repositories only do DB
  access; services own business rules and transactions; routers only do
  HTTP concerns (parse request, call one service method, shape response).
  Don't add a repository method or service layer for something only ever
  called from one place unless it's a real seam (e.g. the provider
  registry, which exists specifically so scrapers are swappable).
- **Sync SQLAlchemy throughout**, matching the pre-existing scraping
  providers' style. DB-bound route handlers are declared `def`, not `async
  def`, so Starlette dispatches them to its threadpool instead of blocking
  the event loop; only routes that `await` real async I/O (OAuth token
  exchange, the provider registry) are `async def`.
- **UUID primary keys**, not autoincrement ints - this is multi-tenant and
  IDs appear in URLs/API responses; sequential IDs leak volume information
  across tenants.
- **Enums are `native_enum=False`** (`app/models/*.py`) - stored as
  `VARCHAR`, portable between Postgres and SQLite, no separate `CREATE
  TYPE` migration dance.
- **Errors are typed** (`app/core/errors.py`: `NotFoundError`,
  `ConflictError`, `QuotaExceededError`, etc.), not raw `HTTPException`,
  so every error response has the same `{type, title, status, code,
  detail}` shape regardless of which layer raised it.
- **Never log/return raw secrets** - API keys and refresh tokens are
  stored hashed (`core/security.hash_token`) and only ever shown once at
  creation time (`ApiKeyCreatedResponse.api_key`,
  `WebhookCreatedResponse.secret`). Webhook endpoint secrets are the one
  deliberate exception (stored plaintext) - see the docstring in
  `app/models/webhook.py` for why (we need the raw value to sign every
  delivery, unlike a bearer credential we only ever compare against).

## Local dev with real OAuth/Stripe credentials

The frontend repo's own `CLAUDE.md`/`STATE.md` track what's currently
configured. Notes specific to *this* backend if you're setting it up
fresh:

- **OAuth**: register a Google Cloud OAuth client with redirect URI
  `{OAUTH_REDIRECT_BASE_URL}/v1/auth/oauth/google/callback` (must match
  exactly). Google apps in "Testing" publish status only let
  explicitly-added test-user accounts log in.
- **Stripe**: get test-mode `sk_test_.../pk_test_...` from the dashboard
  (toggle "Test mode" on first - live-mode keys exist by default on any
  account regardless of activation status, they're just inert). For each
  paid `Plan` row, create a real Stripe Price (`stripe prices create
  --product=... --unit-amount=... --currency=... --recurring.interval=month`)
  and set it as the product's `default_price` before archiving any old
  price (Stripe refuses to archive a product's current default price).
  Write the resulting `price_...` ID into `plans.stripe_price_id`
  directly - no admin endpoint for this.
- **Stripe webhook secret**: for local dev, use the Stripe CLI
  (`stripe listen --forward-to localhost:8000/v1/billing/webhook`) rather
  than a Dashboard-created webhook endpoint - Stripe's servers can't
  reach `localhost` directly, and the CLI forwards real test-mode events
  without needing one. Must stay running for webhook-driven subscription
  sync to work (checkout/portal redirects work fine without it).
- **Two real webhook bugs were found and fixed via a real Stripe
  checkout, not code review alone** - worth knowing if you touch
  `services/billing_service.py` or `routers/v1/billing.py` again:
  1. `create_checkout_session` originally set `client_reference_id`/
     `metadata` only on the Checkout *Session* - Stripe does not copy
     those onto the resulting *Subscription* object, which is what the
     webhook handler reads. Fixed by also passing
     `subscription_data={"metadata": {...}}`.
  2. The webhook handler called `.get(...)` on a raw `stripe.StripeObject`
     (from `event["data"]["object"]`), which doesn't support dict-style
     `.get()` via attribute access in the installed SDK version - crashed
     every subscription webhook with a 500. Fixed with `.to_dict()`
     before any `.get()` calls. **If you add new webhook event handling,
     call `.to_dict()` on the event's data object first** - don't assume
     it behaves like a plain dict.
  3. `tests/conftest.py` now force-blanks `STRIPE_SECRET_KEY` (in
     addition to `DATABASE_URL`/`JWT_SECRET`/etc.) - `Settings` reads the
     real `.env` file directly via `SettingsConfigDict(env_file=...)`, so
     a real key sitting in `.env` for local live-testing was leaking into
     the test process and breaking the "Stripe not configured" test.

## What's deliberately not done yet (see task list this repo shipped with)

- OAuth needs real Google app credentials per environment.
- The provider registry wraps existing scrapers only; a paid aggregator
  (Terminal49/Vizion/project44) is a drop-in adapter away (see above) but
  not implemented, since no such account/credentials exist yet.
- Member invites (`POST /v1/organizations/me/members`) require the invitee
  to already have an account - no email-invite flow (no SMTP provider
  chosen yet).
- ~~No "switch active organization" endpoint~~ - fixed:
  `GET /v1/auth/organizations` lists every org a user belongs to,
  `POST /v1/auth/switch-organization` re-issues a token pair scoped to
  any of them (`AuthService.switch_organization`). Login/signup still
  resolve to a user's *earliest* membership by default
  (`AuthService._primary_organization`) - this is how a user who's a
  member of more than one org reaches the others afterward. See
  `tests/test_auth.py::test_switch_organization_*`.
