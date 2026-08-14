"""Test suite setup.

Sets `DATABASE_URL` to a throwaway SQLite file *before* anything under
`app/` is imported (pydantic-settings reads the real environment at
import time - see app/core/config.py), so the whole test session runs
against an isolated database instead of whatever `.env`/production points
at. Each test gets a freshly-created-and-dropped schema (function-scoped
`_fresh_schema` below) rather than a shared/reused one, trading a bit of
speed for tests that can't leak state into each other via unique-constraint
collisions (duplicate emails/slugs, etc).

Live network calls are never exercised here: `_fake_provider_registry`
(autouse) replaces the container-tracking provider registry with a
canned in-memory fake for every test, so nothing in this suite depends on
SeaRates/Romeu/track-trace.com/proxy credentials being reachable, and
`fake_arq_pool` (autouse) stands in for the arq/Redis pool both services
enqueue onto, so no test needs a Redis either.
"""

from __future__ import annotations

import os

# Defaults to a throwaway SQLite file so the suite never touches a real
# database by accident (every test does a destructive drop_all/create_all
# cycle - see `_fresh_schema` below). To run this exact suite against a
# real Postgres instead (e.g. to confirm Postgres-specific behavior before
# a deploy), opt in explicitly:
#   TEST_DATABASE_URL=postgresql+psycopg://... uv run python -m pytest tests/ -v
# Deliberately a separate variable from `DATABASE_URL` - if a dev happens
# to have that exported in their shell for unrelated local-dev reasons,
# tests must not silently start wiping tables in it.
#
# WARNING: point this at a database you don't mind being emptied.
# `_fresh_schema` drops every app table at the end of each test - by the
# end of a full run, whatever this points at ends up with no app tables
# at all (Alembic's own `alembic_version` bookkeeping table isn't part of
# Base.metadata, so it survives untouched and now *disagrees* with reality -
# `alembic upgrade head` afterwards will think it's already current and do
# nothing. Recover with `alembic stamp base && alembic upgrade head`.
# Use a dedicated test database (e.g. `cargotrack_test`), never the same
# one you point the running app at.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite:///./test_cargotrack.db")
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-for-production-use")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-only-webhook-secret-not-for-production-use")
os.environ.setdefault("CORS_ALLOW_ORIGINS", '["http://testserver"]')
# Settings reads the real .env file directly (see core/config.py's
# SettingsConfigDict(env_file=...)), not just os.environ - so a real
# STRIPE_SECRET_KEY sitting in .env (for local live testing) would
# otherwise leak into the test process and break tests that assert the
# "Stripe not configured" 503 path. Force it blank here regardless of
# what .env has, same reasoning as the vars above.
os.environ["STRIPE_SECRET_KEY"] = ""
# Same reasoning as STRIPE_SECRET_KEY above: a real key sitting in .env for
# local live-testing must not leak into the test process. With email's
# swallow-on-failure design (see services/email_service.py /
# AuthService.signup/forgot_password), signup/forgot-password tests pass
# unchanged with this blank - the send silently no-ops.
os.environ["RESEND_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient

import app.routers.tracking as tracking_router
import app.routers.v1.containers as containers_router
import app.services.container_service as container_service_module
import app.services.webhook_service as webhook_service_module
from app.db.base import Base
from app.db.session import engine
from app.main import app as fastapi_app
from app.providers.base import NormalizedTrackingResult
from app.services.billing_service import seed_default_plans

TEST_DB_PATH = "./test_cargotrack.db"


@pytest.fixture(autouse=True)
def _fresh_schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_db_file():
    yield
    engine.dispose()
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)


class _FakeProviderRegistry:
    """Deterministic stand-in for providers.registry.ProviderRegistry -
    no real HTTP/browser calls, so container-tracking tests are fast and
    hermetic."""

    provider_names = ["fake"]

    def __init__(self):
        self.calls: list[str] = []

    async def track(self, container_number: str) -> NormalizedTrackingResult:
        self.calls.append(container_number)
        if container_number.upper().startswith("MISS"):
            return NormalizedTrackingResult(ok=False, error="not found by fake provider")
        return NormalizedTrackingResult(
            ok=True,
            status="In Transit",
            location="Rotterdam",
            provider_name="fake",
            raw_data={"fake": True},
        )


@pytest.fixture(autouse=True)
def _fake_provider_registry(monkeypatch):
    fake = _FakeProviderRegistry()
    monkeypatch.setattr(containers_router._service, "registry", fake)
    monkeypatch.setattr(tracking_router._service, "registry", fake)
    # The `scrape_container` arq task builds its own fresh ContainerService(),
    # which would otherwise construct the *real* provider registry and make
    # live network calls. Patching the
    # factory covers every such instance, not just the routers' singletons.
    monkeypatch.setattr(container_service_module, "build_default_registry", lambda: fake)
    return fake


class _FakeArqPool:
    def __init__(self) -> None:
        self.enqueued: list[tuple] = []

    async def enqueue_job(self, name, *args, **kwargs) -> None:
        self.enqueued.append((name, args, kwargs))


@pytest.fixture(autouse=True)
def fake_arq_pool(monkeypatch) -> _FakeArqPool:
    """Autouse: nothing in this suite may reach a real Redis.

    Both services enqueue best-effort inside a try/except, so without this
    the suite would still pass - just slowly, against a connection that
    isn't there, with genuine enqueue bugs invisible. Tests that care can
    request this fixture and assert on `.enqueued`.

    Deliberately *not* named `fake_pool`: test_webhook_delivery_worker.py
    has its own module-local fixture by that name (it also fakes httpx and
    patches a different module), and a same-name override there would
    silently disable the container_service patch for that file.
    """
    pool = _FakeArqPool()

    async def _fake_get_pool():
        return pool

    monkeypatch.setattr(container_service_module, "get_arq_pool", _fake_get_pool)
    monkeypatch.setattr(webhook_service_module, "get_arq_pool", _fake_get_pool)
    return pool


@pytest.fixture()
def client():
    with TestClient(fastapi_app) as c:
        yield c


@pytest.fixture()
def db_session():
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def signed_up_org(client, db_session):
    """Signs up a fresh account and returns (tokens, org_slug, headers)."""
    seed_default_plans(db_session)
    resp = client.post(
        "/v1/auth/signup",
        json={
            "email": "founder@example.com",
            "password": "correct-horse-battery-staple",
            "full_name": "Founder",
            "organization_name": "Acme Shipping",
        },
    )
    assert resp.status_code == 200, resp.text
    tokens = resp.json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    return tokens, headers


@pytest.fixture()
def api_key(client, signed_up_org):
    """Creates a sandbox API key for the signed-up org and returns the raw key."""
    _tokens, headers = signed_up_org
    resp = client.post("/v1/api-keys", json={"name": "test key", "mode": "sandbox", "scopes": []}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]
