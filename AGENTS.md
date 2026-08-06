# AGENTS.md

Quick reference for coding agents working in this repo. For architecture,
subsystem boundaries, and the "files that must not change behavior" list,
read **CLAUDE.md first** - this file is commands and conventions only.

## Setup

```bash
uv sync                              # installs from pyproject.toml/uv.lock
cp .env.example .env
docker compose up -d postgres redis  # local Postgres + Redis
uv run alembic upgrade head
```

## Commands

| Task | Command |
|---|---|
| Run the API | `uv run uvicorn app.main:app --reload` |
| Run the background worker (webhooks + poller) | `uv run arq app.workers.arq_app.WorkerSettings` |
| Run tests | `uv run python -m pytest tests/ -v` |
| Run one test file | `uv run python -m pytest tests/test_containers.py -v` |
| New migration | `uv run alembic revision -m "description"` (write it by hand - see CLAUDE.md) |
| Apply migrations | `uv run alembic upgrade head` |
| Roll back one migration | `uv run alembic downgrade -1` |
| Seed dev data (plans + a dev org/user/API key) | `uv run python scripts/init_db.py` |
| Add a dependency | `uv add <package>` (never hand-edit `uv.lock`) |

Always run tests via `uv run python -m pytest`, not bare `pytest` - the
repo root needs to be on `sys.path` for `import app...` to resolve, which
`python -m` guarantees and the `pytest` console script does not.

## Before opening a PR / calling a task done

1. `uv run python -m pytest tests/ -v` - must pass.
2. If you touched `app/models/`, you added a matching file in
   `alembic/versions/` and ran `uv run alembic upgrade head` against a
   throwaway DB to confirm it applies (`DATABASE_URL=sqlite:///./scratch.db
   uv run alembic upgrade head`, then delete the scratch file).
3. `uv run python -c "from app.main import app"` still succeeds with **no**
   `.env` present - every optional integration (Stripe, OAuth) must degrade
   gracefully, not crash import/startup.

## Conventions (see CLAUDE.md for the why)

- Layout: `router → service → repository → model`. Routers hold no
  business logic or raw queries.
- JWT auth (`get_current_session`) for dashboard/account routes;
  `X-API-Key` auth (`get_api_key_principal`) for the product API
  (`/v1/containers`, `/v1/usage`, `/v1/webhooks`). New `/v1` routes must
  pick one deliberately, not by copy-pasting a neighboring route.
- Raise typed errors from `app/core/errors.py`, never bare `HTTPException`,
  inside services/repositories.
- Sync SQLAlchemy; DB-bound route handlers are `def`, not `async def`.
- New carrier/terminal data sources are providers behind
  `app/providers/registry.py`, not new call sites scattered through
  services.
- Never modify `app/providers/searates_http.py` or
  `app/routers/searates_debug.py`. Never remove a route others may
  depend on - deprecate (`deprecated=True`) and alias instead, as
  `app/routers/tracking.py` does for the old `/v1/track*` paths.
- No speculative abstraction: don't add a repository/service method with
  exactly one caller "for later." Three similar lines beat a premature
  interface.
