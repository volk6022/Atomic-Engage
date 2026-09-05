"""get_chat_history — last N posts of a PUBLIC channel (§1-§2 contract, §3.3, §9.2.1).

Public channels are readable without joining. Returns recency + content signals,
author/thread fields (§1), and contacts swept from each post. `limit` is capped at
1000 (§2) — kurigram internally chunks by 100, so a large limit is its own internal
bookkeeping, not 1000 calls of ours. `min_date` (ISO) early-stops the descending scan.
Warmup-exempt, read-budget limited (heaviest read -> smallest budget, see
safety_defaults.READ_LIMITS).

Pagination (§2): kurigram marks `offset_id` deprecated and unconditionally overwrites
it inside `Client.get_chat_history` (`offset_id = max_id if max_id else 0` when not
reversed), so paging by `offset_id` alone silently returned the same page every time —
confirmed by a live run where five pages with different offset_id came back identical.
`max_id` (page backward through history) and `min_id` (poll forward from a cursor) are
the real cursors and are forwarded to kurigram as-is: both are inclusive on the
kurigram side (it internally does `min_id - 1` / `max_id + 1`), so callers page
backward with `max_id = min(ids of the previous page) - 1` without any extra
off-by-one on our end. `offset_id` is still accepted for backward compatibility and
translated to `max_id` with a logged warning, but is NEVER forwarded to kurigram
itself — passing it at all (even as None is fine, but any non-None value) re-triggers
kurigram's own deprecation warning on every single call.
"""
import logging
from datetime import datetime, timezone

from app.workers import _tg_errors as tg
from app.workers._read_helpers import build_post, not_found_reason
from app.workers.base_task import run_task

logger = logging.getLogger(__name__)

_MAX_LIMIT = 1000


def _parse_iso(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _reached_min_date(mdate, min_date) -> bool:
    """Whether this post is already older than the requested window boundary.

    A named helper rather than an inline comparison because the inline one was
    unreachable from any test and shipped broken: kurigram returns `Message.date`
    **naive** (UTC), `_parse_iso` returns it **aware**, and comparing that pair
    raises `TypeError` on the first message of the first page. `min_date` was
    declared, documented and dead for as long as the action has existed — found
    live on 2026-09-05 by Radar's first automatic backfill, and invisible until
    then because nobody had ever passed the parameter.

    Naive dates are read as UTC: that is what Telegram hands over, and guessing a
    local zone here would silently shift every boundary by hours.

    The boundary itself is kept — "the last month" means the month.
    """
    if min_date is None or mdate is None:
        return False
    if mdate.tzinfo is None:
        mdate = mdate.replace(tzinfo=timezone.utc)
    return mdate < min_date


def _effective_max_id(payload: dict, task_id: int) -> int:
    """Resolve the backward-paging cursor: `max_id` wins when both are set; a legacy
    `offset_id` is translated into `max_id` (with a warning) rather than forwarded to
    kurigram, which would otherwise re-log its own deprecation warning per call."""
    max_id = int(payload.get("max_id") or 0)
    offset_id = payload.get("offset_id")
    if offset_id:
        logger.warning(
            "get_chat_history task=%s: offset_id is deprecated, translating to max_id",
            task_id,
        )
        if not max_id:
            max_id = int(offset_id)
    return max_id


async def get_chat_history(ctx, task_id: int) -> dict:
    def builder(payload):
        async def action(client):
            chat_id = payload.get("username") or payload.get("peer_id")
            limit = min(int(payload.get("limit") or 30), _MAX_LIMIT)
            max_id = _effective_max_id(payload, task_id)
            min_id = int(payload.get("min_id") or 0)
            min_date = _parse_iso(payload.get("min_date"))
            offset_date = _parse_iso(payload.get("offset_date"))

            history_kwargs = {"limit": limit, "max_id": max_id, "min_id": min_id}
            if offset_date is not None:
                history_kwargs["offset_date"] = offset_date

            posts: list[dict] = []
            try:
                async for m in client.get_chat_history(chat_id, **history_kwargs):
                    # History is newest-first; max_id/min_id are already applied by
                    # kurigram itself, only the min_date cursor needs an early exit.
                    if _reached_min_date(getattr(m, "date", None), min_date):
                        break
                    posts.append(build_post(m))
            except tg.NOT_FOUND_ERRORS as e:
                return {"found": False, "reason": not_found_reason(e)}

            dates = [p["date"] for p in posts if p["date"]]
            return {
                "found": True,
                "count": len(posts),
                "newest_date": max(dates) if dates else None,
                "oldest_date": min(dates) if dates else None,
                "posts": posts,
            }

        return action

    return await run_task(ctx, task_id, builder, read_action="get_chat_history")
