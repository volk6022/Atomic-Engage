#!/usr/bin/env python3
"""Insert a Task row and enqueue it on the arq queue for the running fleet-worker.

Goes through the REAL worker path (base_task.run_task: prepare gates, FIFO, budget,
humanizer, telemetry) — unlike scripts/send_test_message.py which bypasses the worker.
It does NOT apply the gateway's warmup-tier gate (that lives in the API layer only), so
it can drive a send from a fresh account for testing.

Run:
  .venv/Scripts/python.exe scripts/enqueue_task.py --account-id 6 --action send_message \
      --payload '{"recipient_username": "volk6022", "text": "hi"}'
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid

from app.core.config import get_settings
from app.core.constants import TaskStatus
from app.db.models import Task
from app.db.session import get_session_maker


async def run(args) -> int:
    payload = json.loads(args.payload)
    session_maker = get_session_maker()
    async with session_maker() as db:
        task = Task(
            external_id=str(uuid.uuid4()),
            account_id=args.account_id,
            task_type=args.action,
            payload=payload,
            status=TaskStatus.QUEUED,
            webhook_url=args.webhook_url,
            priority=args.priority,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id
        ext = task.external_id

    from arq import create_pool
    from arq.connections import RedisSettings

    pool = await create_pool(RedisSettings.from_dsn(get_settings().REDIS_URL))
    await pool.enqueue_job(args.action, task_id=task_id)
    print(f"[enqueue] task_id={task_id} external={ext} action={args.action} "
          f"account={args.account_id} payload={payload}", flush=True)
    return 0


def main() -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--action", required=True)
    ap.add_argument("--payload", required=True, help="JSON object")
    ap.add_argument("--webhook-url", default="")
    ap.add_argument("--priority", type=int, default=5)
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
