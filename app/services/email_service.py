"""Transactional email via Resend's REST API.

Called directly over HTTP (`POST https://api.resend.com/emails`) rather
than pulling in the `resend` PyPI package - one more dependency for two
emails isn't worth it when httpx is already used the same way elsewhere
(services/oauth_providers.py, workers/tasks/webhook_delivery.py). A
synchronous `httpx.Client`, not `AsyncClient`, since every caller
(AuthService) is itself sync - see CLAUDE.md on the sync-SQLAlchemy
convention.

`_require_resend()` mirrors `_require_stripe()` in billing_service.py.
For the welcome/password-reset emails, the similarity ends there: those
are side effects of a bigger operation (signup/forgot-password), so a
broken/unconfigured Resend key must never fail that operation, and
forgot-password must always look the same whether or not the send
actually happened (otherwise the response becomes an
account-enumeration oracle) - callers (AuthService) wrap those two calls
in try/except themselves. `send_contact_notification` (the contact-form
endpoint) is different: sending the notification *is* the entire point
of that request, so its caller (ContactService) deliberately does NOT
swallow `FeatureNotConfiguredError` - it's left to propagate up to the
global `AppError` handler (core/errors.py), which turns it into a clean
HTTP 503 instead of a 500. Either way, nothing in this module swallows
errors itself, so failures are still visible to logs/tests that call
these functions directly.
"""

from __future__ import annotations

import httpx

from ..core.config import settings
from ..core.errors import FeatureNotConfiguredError

_RESEND_API_URL = "https://api.resend.com/emails"
_TIMEOUT_S = 10.0


def _require_resend() -> None:
    if not settings.resend_api_key:
        raise FeatureNotConfiguredError("Email is not configured on this server (RESEND_API_KEY is unset).")


def _send(*, to_email: str, subject: str, html: str) -> None:
    _require_resend()
    with httpx.Client(timeout=_TIMEOUT_S) as client:
        response = client.post(
            _RESEND_API_URL,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={"from": settings.resend_from_email, "to": [to_email], "subject": subject, "html": html},
        )
    response.raise_for_status()


def send_welcome_email(*, to_email: str, full_name: str | None) -> None:
    greeting_name = full_name or to_email
    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto;">
      <h1 style="color: #111827; font-size: 20px;">Welcome to CargoTrack, {greeting_name}!</h1>
      <p style="color: #374151; font-size: 14px; line-height: 1.6;">
        Thanks for signing up. CargoTrack keeps your containers tracked and your team
        notified the moment something changes - across every carrier and terminal, in
        one place.
      </p>
      <p style="color: #374151; font-size: 14px; line-height: 1.6;">
        You're all set - head back to your dashboard to start tracking your first container.
      </p>
    </div>
    """
    _send(to_email=to_email, subject="Welcome to CargoTrack", html=html)


def send_password_reset_email(*, to_email: str, reset_url: str, ttl_minutes: int) -> None:
    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto;">
      <h1 style="color: #111827; font-size: 20px;">Reset your password</h1>
      <p style="color: #374151; font-size: 14px; line-height: 1.6;">
        We received a request to reset the password for your CargoTrack account.
        Click the button below to choose a new one. This link expires in
        {ttl_minutes} minutes.
      </p>
      <p style="margin: 24px 0;">
        <a href="{reset_url}"
           style="background: #111827; color: #ffffff; padding: 12px 20px;
                  text-decoration: none; border-radius: 6px; font-size: 14px;
                  display: inline-block;">
          Reset password
        </a>
      </p>
      <p style="color: #6b7280; font-size: 13px; line-height: 1.6;">
        If you didn't request this, you can safely ignore this email - your
        password will not be changed.
      </p>
    </div>
    """
    _send(to_email=to_email, subject="Reset your CargoTrack password", html=html)


def send_contact_notification(*, name: str, email: str, company: str | None, message: str) -> None:
    """Notifies the support inbox (`settings.resend_from_email`, also used
    as the `from` address - there's only one configured mailbox in this
    environment) of a new contact-form submission. Unlike the two emails
    above, this is sent to the *team*, not the visitor who submitted the
    form - see ContactService for why an unconfigured Resend key is
    allowed to surface as a 503 here instead of being swallowed."""
    company_line = f"<p style=\"color: #374151; font-size: 14px;\"><strong>Company:</strong> {company}</p>" if company else ""
    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto;">
      <h1 style="color: #111827; font-size: 20px;">New contact form submission</h1>
      <p style="color: #374151; font-size: 14px;"><strong>Name:</strong> {name}</p>
      <p style="color: #374151; font-size: 14px;"><strong>Email:</strong> {email}</p>
      {company_line}
      <p style="color: #374151; font-size: 14px; line-height: 1.6; white-space: pre-wrap;">{message}</p>
    </div>
    """
    _send(to_email=settings.resend_from_email, subject=f"Contact form: {name}", html=html)
