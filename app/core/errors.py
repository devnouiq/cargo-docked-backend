"""Typed application errors + a single exception handler that turns them
into consistent RFC-7807-flavored JSON responses.

Routers/services raise `AppError` subclasses instead of `HTTPException`
directly, so every error response - regardless of which layer raised it -
has the same `{type, title, status, detail, code}` shape for API
consumers to branch on programmatically via `code` rather than parsing
`detail` strings.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"

    def __init__(self, detail: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(detail)
        self.detail = detail
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class QuotaExceededError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "quota_exceeded"


class UpstreamProviderError(AppError):
    """A carrier/terminal data source failed or rate-limited us."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "upstream_provider_error"


class FeatureNotConfiguredError(AppError):
    """A feature (e.g. billing) is reachable but its provider credentials
    (Stripe keys, OAuth app secrets) aren't set in this environment."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "feature_not_configured"


def _problem_response(*, status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "type": f"https://docs.cargotrack.dev/errors/{code}",
            "title": code.replace("_", " ").title(),
            "status": status_code,
            "code": code,
            "detail": detail,
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return _problem_response(status_code=exc.status_code, code=exc.code, detail=exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="validation_error",
            detail=str(exc.errors()),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger.exception("unhandled error on %s %s (request_id=%s)", request.method, request.url.path, request_id)
        return _problem_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            detail="An unexpected error occurred.",
        )
