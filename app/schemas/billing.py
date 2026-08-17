from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..models.billing import SubscriptionStatus


class PlanOut(BaseModel):
    code: str
    name: str
    monthly_price_cents: int
    # None until a USD price is configured for this plan (Free/Enterprise
    # never get one - see app/models/billing.py::Plan.monthly_price_cents_usd).
    monthly_price_cents_usd: int | None = None
    included_credits: int


class CheckoutSessionRequest(BaseModel):
    plan_code: str
    success_url: str
    cancel_url: str
    currency: Literal["eur", "usd"] = "eur"
    # EU/UK VAT number (e.g. "DE123456789", "GB123456789") for B2B
    # reverse-charge - optional, see BillingService.create_checkout_session.
    vat_number: str | None = None


class CreditCheckoutSessionRequest(BaseModel):
    """One-off pay-per-credit top-up - additive to the org's balance, not
    tied to a Plan/Stripe Price (see BillingService.create_credit_checkout_session)."""

    credits: int = Field(gt=0)
    currency: Literal["eur", "usd"] = "eur"
    success_url: str
    cancel_url: str
    vat_number: str | None = None


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
