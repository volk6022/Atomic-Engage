"""Restart durability (FR-120) and deferred-task scheduling (FR-105).

* recover_orphaned_tasks: on startup, reset tasks stuck in `executing` beyond the
  recovery lease back to `queued` so a crashed worker never blocks an account's FIFO
  queue forever. The lease MUST exceed job_timeout so a genuinely-running task is
  never reset.
* reenqueue_due_deferred: periodically move `deferred` tasks whose `deferred_until`
  has passed back to `queued` and enqueue them (idempotent via a locked transition).

Together they give at-least-once eventual execution of every non-terminal task after
an unplanned restart.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.core.constants import TaskStatus
from app.db.models import Task

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 600  # > job_timeout (300)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def recover_orphaned_tasks(db, lease_seconds: int = DEFAULT_LEASE_SECONDS, redis=None) -> list[int]:
    """Reset stale `executing` tasks to `queued`; optionally re-enqueue them.

    Returns the list of recovered task ids.
    """
    cutoff = _now() - timedelta(seconds=lease_seconds)
    rows = (
        await db.execute(
            select(Task.id, Task.account_id, Task.task_type).where(
                Task.status == TaskStatus.EXECUTING, Task.started_at < cutoff
            )
        )
    ).all()
    if not rows:
        return []

    ids = [r[0] for r in rows]
    await db.execute(
        update(Task)
        .where(Task.id.in_(ids))
        .values(status=TaskStatus.QUEUED, started_at=None)
    )
    await db.commit()
    logger.warning("recovered_orphaned_tasks count=%s ids=%s", len(ids), ids)

    if redis is not None:
        for _id, _account, task_type in rows:
            try:
                await redis.enqueue_job(task_type, task_id=_id)
            except Exception:  # pragma: no cover - best effort
                logger.warning("recover_enqueue_failed task=%s", _id)
    return ids


async def reenqueue_due_deferred(db, redis=None, limit: int = 100) -> list[int]:
    """Move due `deferred` tasks back to `queued` and enqueue them (idempotent)."""
    due = (
        await db.execute(
            select(Task)
            .where(Task.status == TaskStatus.DEFERRED, Task.deferred_until <= _now())
            .order_by(Task.priority.desc(), Task.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()

    enqueued = []
    for task in due:
        task.status = TaskStatus.QUEUED
        task.deferred_until = None
        enqueued.append((task.id, task.task_type))
    if due:
        await db.commit()

    if redis is not None:
        for _id, task_type in enqueued:
            try:
                await redis.enqueue_job(task_type, task_id=_id)
            except Exception:  # pragma: no cover - best effort
                logger.warning("deferred_enqueue_failed task=%s", _id)
    return [i for i, _ in enqueued]
