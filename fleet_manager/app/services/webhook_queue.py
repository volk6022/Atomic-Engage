"""Hand a webhook to the queue instead of delivering it inline.

Delivery used to be awaited inside whatever was producing the event, with a
30/60/120/240 s backoff. One unreachable receiver therefore pinned a worker slot for
seven and a half minutes, and with a system webhook pointing at a host that did not
resolve the whole fleet crawled. Producers now persist a `WebhookDelivery` row and
enqueue `deliver_webhook`; the row is the durable record, so an event survives even if
the enqueue itself fails and can be re-driven later.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.core.config import get_settings
from app.db.models import WebhookDelivery

logger = logging.getLogger(__name__)

_pool = None
_pool_lock = asyncio.Lock()


async def _get_pool():
    """One arq pool per process, created on first use.

    The producers here are the watcher (one call per incoming Telegram message) and the
    task path, so opening a pool per call would mean a new Redis connection per message.
    """
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                from arq import create_pool
                from arq.connections import RedisSettings

                _pool = await create_pool(
                    RedisSettings.from_dsn(get_settings().REDIS_URL)
                )
    return _pool


async def enqueue_webhook(db, url: Optional[str], payload: dict) -> Optional[int]:
    """Persist the delivery, then queue it. Returns the delivery id (None if no url).

    Commits: the row must be durable before the job can run, or the worker would look
    up an id that is not visible to it yet.
    """
    if not url:
        return None

    delivery = WebhookDelivery(url=url, payload=payload, status="pending", attempts=0)
    db.add(delivery)
    await db.flush()
    delivery_id = delivery.id
    await db.commit()

    try:
        pool = await _get_pool()
        await pool.enqueue_job("deliver_webhook", delivery_id)
    except Exception as e:  # noqa: BLE001 — a queue outage must not lose the event
        # The row stays `pending`, so nothing is silently dropped: it is visible in
        # webhook_deliveries and can be re-driven. Loud, because a webhook that never
        # leaves is a lead that never arrives.
        logger.error(
            "webhook_enqueue_failed delivery_id=%s url=%s err=%s", delivery_id, url, e
        )
    return delivery_id
