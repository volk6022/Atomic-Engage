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


async def _create_pool():
    """Build one arq pool. Separate from the cache so the cache can be dropped and
    rebuilt without either half knowing about the other."""
    from arq import create_pool
    from arq.connections import RedisSettings

    return await create_pool(RedisSettings.from_dsn(get_settings().REDIS_URL))


def reset_pool() -> None:
    """Drop the cached pool so the next call builds a fresh one.

    A cached pool outlives the connection it wraps. Until this existed, one Redis
    blip poisoned the process for its whole lifetime: the `except` below logged the
    failure and left the dead object in `_pool`, so every following webhook failed on
    the same corpse while the process still looked healthy from the outside. That is
    the shape of the 18.08 outage — a fleet that reports fine and delivers nothing.

    Closing is best-effort and deliberately swallowed: the pool we are dropping is by
    definition the one we no longer trust, and failing to close it must not stop the
    next webhook from getting a working one.
    """
    global _pool
    dead, _pool = _pool, None
    close = getattr(dead, "close", None)
    if close is not None:
        try:
            result = close()
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)
        except Exception:  # noqa: BLE001 — dropping a pool must never raise
            logger.debug("webhook_pool_close_failed", exc_info=True)


async def _get_pool():
    """One arq pool per process, created on first use.

    The producers here are the watcher (one call per incoming Telegram message) and the
    task path, so opening a pool per call would mean a new Redis connection per message.

    A failed build is not cached: `_pool` is only assigned once the pool exists, so a
    Redis outage during startup leaves the next call free to try again.
    """
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = await _create_pool()
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
        # Drop the pool first: whatever just failed, the cached connection is now
        # suspect, and keeping it would turn one blip into a permanently mute process.
        reset_pool()
        # The row stays `pending`, so nothing is silently dropped: it is visible in
        # webhook_deliveries and re-driven by `webhook_redrive_tick`. Loud, because a
        # webhook that never leaves is a lead that never arrives.
        logger.error(
            "webhook_enqueue_failed delivery_id=%s url=%s err=%s", delivery_id, url, e
        )
    return delivery_id
