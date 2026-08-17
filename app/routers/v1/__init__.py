"""Aggregates every standardized `/v1` router into one importable object so
app/main.py does a single `app.include_router(v1_router)` instead of one
per resource - new resources register themselves here, not in main.py.
"""

from fastapi import APIRouter

from . import api_keys, auth, billing, contact, containers, organizations, status, usage, webhooks

v1_router = APIRouter()
v1_router.include_router(auth.router)
v1_router.include_router(organizations.router)
v1_router.include_router(api_keys.router)
v1_router.include_router(containers.router)
v1_router.include_router(usage.router)
v1_router.include_router(webhooks.router)
v1_router.include_router(billing.router)
v1_router.include_router(contact.router)
v1_router.include_router(status.router)

__all__ = ["v1_router"]
