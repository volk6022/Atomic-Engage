"""get_chat_history — last N posts of a PUBLIC channel (§3.3, §9.2.1).

Public channels are readable without joining. Returns recency + content signals +
contacts swept from each post. `limit` is hard-capped at 50; `min_date` (ISO) and
`min_id` (last-seen cursor for incremental polling) both early-stop the descending
scan. Warmup-exempt, read-budget limited (heaviest read -> smallest budget).
"""
from datetime import datetime, timezone

from app.workers import _tg_errors as tg
from app.workers._read_helpers import build_post
from app.workers.base_task import run_task

_MAX_LIMIT = 50


def _parse_min_date(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def get_chat_history(ctx, task_id: int) -> dict:
    def builder(payload):
        async def action(client):
            chat_id = payload.get("username") or payload.get("peer_id")
            limit = min(int(payload.get("limit") or 30), _MAX_LIMIT)
            offset_id = int(payload.get("offset_id") or 0)
            min_id = int(payload.get("min_id") or 0)
            min_date = _parse_min_date(payload.get("min_date"))

            posts: list[dict] = []
            try:
                async for m in client.get_chat_history(
                    chat_id, limit=limit, offset_id=offset_id
                ):
                    # History is newest-first: once we cross either cursor, stop.
                    if min_id and getattr(m, "id", 0) <= min_id:
                        break
                    mdate = getattr(m, "date", None)
                    if min_date and mdate is not None and mdate < min_date:
                        break
                    posts.append(build_post(m))
            except tg.NOT_FOUND_ERRORS:
                return None  # not a public/readable channel -> clean "no data"

            dates = [p["date"] for p in posts if p["date"]]
            return {
                "count": len(posts),
                "newest_date": max(dates) if dates else None,
                "oldest_date": min(dates) if dates else None,
                "posts": posts,
            }

        return action

    return await run_task(ctx, task_id, builder, read_action="get_chat_history")
