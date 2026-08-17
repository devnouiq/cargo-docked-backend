"""Contact-form submission handling.

No DB persistence, no repository - a submission's only effect is a
notification email, so a repository/model layer here would be exactly
the "speculative abstraction" CLAUDE.md's conventions warn against (a
real seam - like the provider registry - would justify one; this
wouldn't be called from anywhere else). If the product ever needs to
list/audit past submissions, that's the point to add a
ContactMessage model + repository, not before.

Deliberately does NOT catch `FeatureNotConfiguredError` from
email_service - see that module's docstring for why: unlike the
welcome/password-reset emails (side effects of a bigger operation),
sending the notification *is* this request's entire purpose, so letting
it propagate to the global AppError handler (a clean 503) is the
correct "graceful degrade" here, not a swallowed no-op.
"""

from __future__ import annotations

from . import email_service


class ContactService:
    def submit(self, *, name: str, email: str, company: str | None, message: str) -> None:
        email_service.send_contact_notification(name=name, email=email, company=company, message=message)
