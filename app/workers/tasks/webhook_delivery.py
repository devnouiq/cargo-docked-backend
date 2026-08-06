"""arq task: deliver one webhook attempt, with HMAC signing and
exponential-backoff retry up to `settings.webhook_max_attempts`.

Retry state lives in `WebhookDelivery.attempt_count` (our own field, not
arq's internal retry counter) so `GET /v1/webhooks/{id}/deliveries` shows
a business-meaningful status (`pending`/`failed`/`success`/`exhausted`)
independent of how arq itself schedules the job.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from ...core.config import settings
from ...core.security import sign_webhook_payload
from ...db.session import SessionLocal
from ...models.webhook import WebhookDeliveryStatus, WebhookEndpoint
from ...repositories.webhooks import WebhookRepository
from ..redis_pool import get_arq_pool

logger = logging.getLogger(__name__)
_repo = WebhookRepository()


def _backoff_seconds(attempt_number: int) -> int:
    return min(600, 30 * (2**attempt_number))  # 30s, 60s, 120s, ... capped at 10m


async def deliver_webhook(ctx: dict, delivery_id: str) -> None:
    with SessionLocal() as db:
        delivery = _repo.get_delivery(db, uuid.UUID(delivery_id))
        if delivery is None:
            logger.warning("webhook delivery %s no longer exists, skipping", delivery_id)
            return

        endpoint = db.get(WebhookEndpoint, delivery.webhook_endpoint_id)
        if endpoint is None or not endpoint.is_active:
            _repo.record_attempt(
                db, delivery, status=WebhookDeliveryStatus.EXHAUSTED,
                response_status_code=None, response_body_snippet="endpoint deleted or disabled",
                next_attempt_at=None,
            )
            return

        body = json.dumps(delivery.payload, default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-CargoTrack-Signature": sign_webhook_payload(payload=body, secret=endpoint.secret),
            "X-CargoTrack-Event": delivery.event_type,
            "X-CargoTrack-Delivery-Id": str(delivery.id),
        }

        try:
            async with httpx.AsyncClient(timeout=settings.webhook_delivery_timeout_s) as client:
                response = await client.post(endpoint.url, content=body, headers=headers)
            success = 200 <= response.status_code < 300
            status_code: int | None = response.status_code
            snippet = response.text[:1000]
        except Exception as exc:  # noqa: BLE001 - any transport failure counts as a failed attempt
            success = False
            status_code = None
            snippet = str(exc)[:1000]

        if success:
            _repo.record_attempt(
                db, delivery, status=WebhookDeliveryStatus.SUCCESS,
                response_status_code=status_code, response_body_snippet=snippet, next_attempt_at=None,
            )
            return

        attempts_after_this_one = delivery.attempt_count + 1
        if attempts_after_this_one >= settings.webhook_max_attempts:
            _repo.record_attempt(
                db, delivery, status=WebhookDeliveryStatus.EXHAUSTED,
                response_status_code=status_code, response_body_snippet=snippet, next_attempt_at=None,
            )
            logger.warning("webhook delivery %s exhausted after %d attempts", delivery_id, attempts_after_this_one)
            return

        backoff = _backoff_seconds(delivery.attempt_count)
        next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=backoff)
        _repo.record_attempt(
            db, delivery, status=WebhookDeliveryStatus.FAILED,
            response_status_code=status_code, response_body_snippet=snippet, next_attempt_at=next_attempt_at,
        )

    pool = await get_arq_pool()
    await pool.enqueue_job("deliver_webhook", delivery_id, _defer_by=backoff)
