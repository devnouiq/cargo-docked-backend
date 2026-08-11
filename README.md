# CargoTrack API

Real-time ocean container tracking API: track containers across carriers,
get milestone events, register webhooks for status changes, and meter
usage against credit-based plans. FastAPI + PostgreSQL + Redis (arq).

This is the backend only - the marketing site and dashboard frontend live
in a separate repository and consume this API.

For architecture, subsystem boundaries, and contributor/agent conventions,
see **[CLAUDE.md](./CLAUDE.md)** and **[AGENTS.md](./AGENTS.md)**.

## Quickstart

```bash
uv sync
cp .env.example .env                  # fill in OXYLABS_* to enable live carrier lookups
docker compose up -d postgres redis
uv run alembic upgrade head
uv run python scripts/init_db.py      # seeds billing plans + a dev org/user/API key
uv run uvicorn app.main:app --reload
```

API docs: http://localhost:8000/docs

In a second terminal, run the background worker (webhook delivery + the
carrier-refresh poller):

```bash
uv run arq app.workers.arq_app.WorkerSettings
```

## Core API

Authenticate with `X-API-Key: <key>` (from `POST /v1/api-keys`, which
itself needs a dashboard session - see "Auth" below).

```bash
# Start tracking a container
curl -X POST http://localhost:8000/v1/containers \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"container_number": "MSKU1234567"}'

# Current status
curl http://localhost:8000/v1/containers/MSKU1234567 -H "X-API-Key: $API_KEY"

# Milestone timeline
curl http://localhost:8000/v1/containers/MSKU1234567/events -H "X-API-Key: $API_KEY"

# Bulk
curl -X POST http://localhost:8000/v1/containers/bulk \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"container_numbers": ["MSKU1234567", "ROMU2210313"]}'

# Register a webhook
curl -X POST http://localhost:8000/v1/webhooks \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/hooks/cargotrack", "event_types": ["container.updated", "container.arrived"]}'

# Usage/credits
curl http://localhost:8000/v1/usage -H "X-API-Key: $API_KEY"
```

Full route list: `/docs` (Swagger) or `/redoc`.

## Auth

Dashboard/account management (signup, org/member management, API keys,
billing) uses JWT sessions, not API keys:

```bash
curl -X POST http://localhost:8000/v1/auth/signup -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "password": "correct-horse-battery-staple", "organization_name": "Acme"}'
# -> {"access_token": "...", "refresh_token": "...", ...}

curl -X POST http://localhost:8000/v1/api-keys -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" -d '{"name": "prod key", "mode": "live", "scopes": []}'
# -> {"api_key": "ctk_live_...", ...}  (shown once)
```

Google OAuth is available via `GET /v1/auth/oauth/{provider}/authorize`
once `GOOGLE_CLIENT_ID` (+ secret) is set in `.env`.

## Testing

```bash
uv run python -m pytest tests/ -v
```

No real database/Redis/network required - see CLAUDE.md "Testing".

## Deploying

Builds via the included `Dockerfile` (runs `alembic upgrade head` before
starting `uvicorn` - fine for a single instance; run migrations as a
separate release step for multi-replica deploys). Requires `DATABASE_URL`
(Postgres) and `REDIS_URL` in the environment; everything else has a
default or degrades gracefully when unset (see `.env.example`).
