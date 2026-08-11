from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from ..models.billing import SubscriptionStatus


class PlanOut(BaseModel):
    code: str
    name: str
    monthly_price_cents: int
    included_credits: int


class CheckoutSessionRequest(BaseModel):
    plan_code: str
    success_url: str
    cancel_url: str


class CheckoutSessionResponse(BaseModel):
    checkout_url: str


class PortalSessionRequest(BaseModel):
    return_url: str


class PortalSessionResponse(BaseModel):
    portal_url: str


class SubscriptionOut(BaseModel):
    plan_code: str
    status: SubscriptionStatus
    current_period_end: datetime | None


class InvoiceOut(BaseModel):
    id: str
    number: str | None
    description: str
    status: str
    amount_cents: int
    currency: str
    created_at: datetime
    hosted_invoice_url: str | None
    invoice_pdf: str | None


class PaymentMethodOut(BaseModel):
    brand: str
    last4: str
    exp_month: int
    exp_year: int
