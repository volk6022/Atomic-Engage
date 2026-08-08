"""deliver_webhook — retrying webhook delivery, off the task path.

Each attempt is one POST that records its outcome on the `WebhookDelivery` row and, if
it failed and attempts remain, re-queues itself with arq's `_defer_by`. Nothing sleeps,
so a dead receiver costs a queue slot for the length of one HTTP timeout rather than
holding a worker for the full backoff.

`webhook_deliveries` was previously a table the code never wrote to -- `attempts`,
`status` and `delivered_at` were dead columns and a dropped webhook left no trace at
all. It is now the delivery log.
"""
import logging
from datetime import timedelta

from app.core.clock import get_clock
from app.db.models import WebhookDelivery
from app.services.webhook_sender import MAX_ATTEMPTS, WEBHOOK_BACKOFF, WebhookSender

logger = logging.getLogger(__name__)


def _backoff_seconds(attempts: int) -> int:
    """Delay before attempt N+1, saturating at the last configured step."""
    idx = min(max(attempts - 1, 0), len(WEBHOOK_BACKOFF) - 1)
    return WEBHOOK_BACKOFF[idx]


async def deliver_webhook(ctx, delivery_id: int) -> dict:
    session_maker = ctx["session_maker"]
    redis = ctx.get("redis")

    async with session_maker() as db:
        delivery = await db.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return {"error": "delivery_not_found", "delivery_id": delivery_id}
        if delivery.status == "delivered":
            return {"already_delivered": True, "delivery_id": delivery_id}

        ok, detail = await WebhookSender().send_once(delivery.url, delivery.payload)
        delivery.attempts += 1
        now = get_clock().now()

        if ok:
            delivery.status = "delivered"
            delivery.delivered_at = now
            await db.commit()
            return {"delivered": True, "attempts": delivery.attempts}

        if delivery.attempts >= MAX_ATTEMPTS:
            delivery.status = "failed"
            await db.commit()
            logger.error(
                "webhook_exhausted delivery_id=%s url=%s attempts=%s last=%s",
                delivery_id, delivery.url, delivery.attempts, detail,
            )
            return {"delivered": False, "exhausted": True, "attempts": delivery.attempts}

        delay = _backoff_seconds(delivery.attempts)
        delivery.status = "pending"
        delivery.next_attempt_at = now + timedelta(seconds=delay)
        await db.commit()

        if redis is not None:
            await redis.enqueue_job(
                "deliver_webhook", delivery_id, _defer_by=timedelta(seconds=delay)
            )
        else:
            # No pool means no retry can be scheduled; the row stays pending and
            # visible rather than the failure vanishing into a log line.
            logger.error(
                "webhook_retry_unschedulable delivery_id=%s attempts=%s",
                delivery_id, delivery.attempts,
            )
        return {"delivered": False, "retry_in": delay, "attempts": delivery.attempts}
