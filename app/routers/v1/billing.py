"""Billing - JWT-authenticated (dashboard) for everything except the
Stripe webhook receiver itself, which Stripe calls directly and
authenticates via signature header, not our auth.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request

from ...core.errors import AppError, NotFoundError
from ...db.session import get_db
from ...dependencies import CurrentSession, get_current_session
from ...schemas.billing import (
    CheckoutSessionRequest,
    CheckoutSessionResponse,
    PlanOut,
    PortalSessionRequest,
    PortalSessionResponse,
    SubscriptionOut,
)
from ...services.billing_service import BillingService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/billing", tags=["billing"])
_service = BillingService()


@router.get("/plans", response_model=list[PlanOut])
def list_plans(db=Depends(get_db)):
    return _service.list_plans(db)


@router.get("/subscription", response_model=SubscriptionOut)
def get_subscription(session: CurrentSession = Depends(get_current_session), db=Depends(get_db)):
    subscription = _service.get_subscription(db, session.organization.id)
    if subscription is None:
        raise NotFoundError("This organization has no subscription yet (still on the default Free allotment).")
    return SubscriptionOut(plan_code=subscription.plan.code, status=subscription.status, current_period_end=subscription.current_period_end)


@router.post("/checkout-session", response_model=CheckoutSessionResponse)
def create_checkout_session(
    payload: CheckoutSessionRequest, session: CurrentSession = Depends(get_current_session), db=Depends(get_db)
):
    url = _service.create_checkout_session(
        db, organization_id=session.organization.id, plan_code=payload.plan_code,
        success_url=payload.success_url, cancel_url=payload.cancel_url,
    )
    return CheckoutSessionResponse(checkout_url=url)


@router.post("/portal-session", response_model=PortalSessionResponse)
def create_portal_session(
    payload: PortalSessionRequest, session: CurrentSession = Depends(get_current_session), db=Depends(get_db)
):
    url = _service.create_billing_portal_session(db, organization_id=session.organization.id, return_url=payload.return_url)
    return PortalSessionResponse(portal_url=url)


@router.post("/webhook", status_code=200, include_in_schema=False)
async def stripe_webhook(request: Request, db=Depends(get_db)):
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = _service.construct_webhook_event(payload=payload, signature=signature)
    except AppError:
        raise
    except Exception as exc:
        logger.warning("rejected stripe webhook: invalid signature (%s)", exc)
        return {"received": False}

    if event["type"] in ("customer.subscription.created", "customer.subscription.updated"):
        stripe_subscription = event["data"]["object"]
        organization_id = stripe_subscription.get("metadata", {}).get("organization_id") or stripe_subscription.get(
            "client_reference_id"
        )
        if organization_id:
            _service.upsert_subscription_from_stripe_object(
                db, organization_id=uuid.UUID(organization_id), stripe_subscription=stripe_subscription
            )
        else:
            logger.warning("stripe subscription event %s had no organization_id in metadata", stripe_subscription.get("id"))

    return {"received": True}
