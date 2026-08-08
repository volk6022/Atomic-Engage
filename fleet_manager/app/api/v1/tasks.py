"""Task read endpoints.

`POST /v1/action` hands back an `external_id` and nothing else: the result was
delivered by webhook or not at all. A dropped webhook therefore meant a permanently
unreachable result, even though it sits right there in `tasks.result`. These endpoints
expose it, so a caller can poll (or reconcile after an outage) instead of trusting a
single at-most-once delivery.

Callers only ever see `external_id` — the internal autoincrement id is not part of the
API surface and is not accepted here.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api import deps
from app.db.models import Task

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _iso(value):
    return value.isoformat() if value is not None else None


def _summary(task: Task) -> dict:
    """List shape: everything except `result`, which routinely runs to hundreds of
    kilobytes (a 1000-message history) and would make listing unusable."""
    return {
        "task_id": task.external_id,
        "account_id": task.account_id,
        "action": task.task_type,
        "status": task.status,
        "error_code": task.error_code,
        "priority": task.priority,
        "retry_count": task.retry_count,
        "created_at": _iso(task.created_at),
        "started_at": _iso(task.started_at),
        "updated_at": _iso(task.updated_at),
    }


@router.get("/{external_id}")
async def get_task(external_id: str, db: deps.GetDB, api_key: deps.VerifyAPIKey):
    task = (
        await db.execute(select(Task).where(Task.external_id == external_id))
    ).scalar_one_or_none()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    return {**_summary(task), "result": task.result}


@router.get("/")
async def list_tasks(
    db: deps.GetDB,
    api_key: deps.VerifyAPIKey,
    account_id: Optional[int] = None,
    task_status: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
):
    stmt = select(Task)
    if account_id is not None:
        stmt = stmt.where(Task.account_id == account_id)
    if task_status:
        stmt = stmt.where(Task.status == task_status)
    # Newest first by id: `created_at` is not a reliable sort key on databases created
    # before migration 0004 froze-default fix, where every row shares one timestamp.
    stmt = stmt.order_by(Task.id.desc()).limit(limit)

    rows = (await db.execute(stmt)).scalars().all()
    return {"count": len(rows), "tasks": [_summary(t) for t in rows]}
