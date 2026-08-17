"""Public contact-form submission - no auth (same "no X-API-Key/Bearer
requirement" shape as routers/searates_debug.py), but unlike that debug
router this one is typed/validated (Pydantic request schema) and lives
under the standardized /v1 API rather than as an internal-only route."""

from __future__ import annotations

from fastapi import APIRouter

from ...schemas.contact import ContactRequest, ContactResponse
from ...services.contact_service import ContactService

router = APIRouter(prefix="/v1/contact", tags=["contact"])
_service = ContactService()


@router.post("", response_model=ContactResponse, status_code=201)
def submit_contact_form(payload: ContactRequest) -> ContactResponse:
    _service.submit(name=payload.name, email=payload.email, company=payload.company, message=payload.message)
    return ContactResponse()
