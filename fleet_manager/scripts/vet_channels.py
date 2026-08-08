#!/usr/bin/env python3
"""Vet candidate job/freelance channels through the fleet READ actions.

For each username: enqueue a real `get_chat_info` + `get_chat_history` task on the
running fleet-worker (account id=6, host IP), poll `task.result` from the DB, then
score against the acceptance criteria:

  * active            -> newest post age (from get_chat_history dates)
  * >1000 members     -> members_count (from get_chat_info)
  * not an hh-reposter -> share of recent posts mentioning hh.ru
  * >6 months old      -> can't read creation date directly; flagged for manual note

Run:
  .venv/Scripts/python.exe scripts/vet_channels.py --account-id 6 \
      --channels ru_pythonjobs remote_ai_jobs ...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.core.constants import TaskStatus
from app.db.models import Task
from app.db.session import get_session_maker

HH_RE = re.compile(r"hh\.ru|headhunter|хедхантер", re.IGNORECASE)


async def _enqueue(db, pool, account_id: int, action: str, payload: dict) -> int:
    task = Task(
        external_id=str(uuid.uuid4()),
        account_id=account_id,
        task_type=action,
        payload=payload,
        status=TaskStatus.QUEUED,
        webhook_url="",
        priority=3,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    tid = task.id
    await pool.enqueue_job(action, task_id=tid)
    return tid


async def _await_result(session_maker, task_id: int, timeout: float = 120.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with session_maker() as db:
            t = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
            if t.status in (TaskStatus.COMPLETE, TaskStatus.FAILED):
                return t.status, t.result, t.error_code
        await asyncio.sleep(3)
    return "timeout", None, None


def _score_history(posts: list[dict]) -> dict:
    dates = []
    hh_hits = 0
    for p in posts or []:
        if p.get("date"):
            try:
                dates.append(datetime.fromisoformat(p["date"]))
            except ValueError:
                pass
        if HH_RE.search(p.get("text") or ""):
            hh_hits += 1
    newest = max(dates) if dates else None
    age_days = None
    if newest is not None:
        now = datetime.now(timezone.utc)
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        age_days = (now - newest).total_seconds() / 86400
    n = len(posts or [])
    return {
        "posts": n,
        "newest_age_days": round(age_days, 2) if age_days is not None else None,
        "hh_share": round(hh_hits / n, 2) if n else None,
    }


async def run(args) -> int:
    from arq import create_pool
    from arq.connections import RedisSettings

    session_maker = get_session_maker()
    pool = await create_pool(RedisSettings.from_dsn(get_settings().REDIS_URL))

    report = []
    for ch in args.channels:
        u = ch.lstrip("@")
        row = {"channel": u}
        async with session_maker() as db:
            info_tid = await _enqueue(db, pool, args.account_id, "get_chat_info", {"username": u})
        st, res, err = await _await_result(session_maker, info_tid)
        if st != TaskStatus.COMPLETE or not res:
            row["status"] = f"info_{st}"
            row["error"] = (err or "")[:120]
            report.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            await asyncio.sleep(args.gap)
            continue
        row["title"] = res.get("title")
        row["type"] = res.get("type")
        row["members"] = res.get("members_count")
        row["verified"] = res.get("is_verified")
        row["scam"] = res.get("is_scam")
        row["linked_chat"] = res.get("linked_chat_username")
        desc = (res.get("description") or "")[:160]
        row["desc"] = desc

        async with session_maker() as db:
            hist_tid = await _enqueue(
                db, pool, args.account_id, "get_chat_history",
                {"username": u, "limit": args.history},
            )
        st2, res2, err2 = await _await_result(session_maker, hist_tid)
        posts = (res2 or {}).get("posts") if isinstance(res2, dict) else res2
        row.update(_score_history(posts if isinstance(posts, list) else []))
        row["status"] = "ok"
        report.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        await asyncio.sleep(args.gap)

    print("\n=== SUMMARY ===", flush=True)
    for r in report:
        m = r.get("members")
        age = r.get("newest_age_days")
        verdict = "?"
        if r.get("status") == "ok":  # noqa
            ok = (isinstance(m, int) and m > 1000
                  and age is not None and age < 7
                  and (r.get("hh_share") or 0) < 0.5)
            verdict = "PASS" if ok else "review"
        else:
            verdict = "FAIL"
        print(f"[{verdict:6}] @{r['channel']:<22} members={m} "
              f"newest_age_d={age} hh_share={r.get('hh_share')} "
              f"type={r.get('type')} :: {r.get('title') or r.get('status')}", flush=True)
    return 0


def main() -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-id", type=int, default=6)
    ap.add_argument("--channels", nargs="+", required=True)
    ap.add_argument("--history", type=int, default=30)
    ap.add_argument("--gap", type=float, default=8.0, help="seconds between channels")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
