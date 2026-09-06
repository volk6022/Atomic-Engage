"""webhook_redrive — the executor behind the promise that a `pending` row can be re-driven.

`webhook_queue.enqueue_webhook` persists a `WebhookDelivery` before queueing it and,
when the queue is unreachable, leaves the row `pending` with a comment saying it "can
be re-driven later". Nothing re-drove it: the worker had three cron jobs and none of
them looked at `webhook_deliveries`. A single Redis blip therefore turned into rows
that sit `pending` forever, and for Radar a stuck row is a lead that never arrived.

There are two roads into that state and this sweep covers both:

* the enqueue itself failed (`webhook_enqueue_failed`), so no job exists at all;
* `deliver_webhook` ran without a pool (`webhook_retry_unschedulable`), so the retry
  it scheduled was never scheduled.

**What counts as stranded.** A row is due when its own clock says so — `next_attempt_at`
for a row that has already been tried, `created_at` for one that never left — and that
moment is older than `REDRIVE_AFTER_SECONDS`. The window is what keeps this sweep from
racing the normal retry schedule: `WEBHOOK_BACKOFF` tops out at 480 s, so a shorter
window would overtake a legitimately-deferred job and deliver the same webhook twice.

**Re-driving marks its own next attempt.** The sweep does not touch `status` — the row
is already `pending`, and pretending otherwise would lose the distinction between "not
delivered yet" and "being retried". Instead it pushes `next_attempt_at` a full window
forward, which makes the sweep idempotent across ticks: a slow receiver is retried on
the sweep's cadence rather than re-queued every five minutes.

**Exhausted rows are left alone.** `attempts >= MAX_ATTEMPTS` is a decision already
made by `deliver_webhook`; re-driving past it would quietly convert a bounded retry
policy into an unbounded one.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.db.models import WebhookDelivery
from app.services.webhook_sender import MAX_ATTEMPTS

logger = logging.getLogger(__name__)

# Must stay strictly greater than the last backoff step (480 s) — see the module
# docstring. 900 s also matches the task recovery lease, so both sweeps answer
# "how long may something be silent before we assume it is stuck" the same way.
REDRIVE_AFTER_SECONDS = 900


async def redrive_pending_webhooks(db, redis=None, limit: int = 100,
                                   now: datetime | None = None) -> list[int]:
    """Re-queue deliveries stuck in `pending`. Returns the ids actually queued.

    The return value is what was really handed to the queue, not what was found: with
    no pool there is nowhere to hand them, and reporting them as driven would hide the
    very outage this function exists to survive.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(seconds=REDRIVE_AFTER_SECONDS)

    rows = (await db.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.status == "pending",
               WebhookDelivery.attempts < MAX_ATTEMPTS,
               or_(WebhookDelivery.next_attempt_at <= cutoff,
                   WebhookDelivery.next_attempt_at.is_(None)
                   & (WebhookDelivery.created_at <= cutoff)))
        .order_by(WebhookDelivery.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )).scalars().all()

    if not rows:
        return []

    if redis is None:
        logger.error("webhook_redrive_unschedulable stranded=%s ids=%s",
                     len(rows), [r.id for r in rows])
        return []

    driven: list[int] = []
    for row in rows:
        try:
            await redis.enqueue_job("deliver_webhook", row.id)
        except Exception:  # noqa: BLE001 — one bad row must not stop the sweep
            logger.warning("webhook_redrive_enqueue_failed delivery_id=%s", row.id)
            continue
        row.next_attempt_at = moment + timedelta(seconds=REDRIVE_AFTER_SECONDS)
        driven.append(row.id)

    await db.commit()
    if driven:
        logger.warning("webhook_redrive count=%s ids=%s", len(driven), driven)
    return driven
