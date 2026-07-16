"""get_chat_info — public profile of a user/bot/channel/group (§3.2).

Read-only enrichment: About text, subscriber count, verified/scam flags, pinned
message, linked discussion chat, plus a URL/email/phone sweep over the free text.
Warmup-exempt, read-budget limited, and cached (Redis, ~7d) for re-runnable
enrichment chains (resolve -> info -> history).
"""
from app.db import redis_client as rc
from app.workers import _tg_errors as tg
from app.workers._read_helpers import build_chat_info
from app.workers.base_task import run_task


async def get_chat_info(ctx, task_id: int) -> dict:
    def builder(payload):
        async def action(client):
            chat_id = payload.get("username") or payload.get("peer_id")
            try:
                chat = await client.get_chat(chat_id)
            except tg.NOT_FOUND_ERRORS:
                return None  # handle gone/private -> clean "no data"
            if not chat:
                return None
            members_count = getattr(chat, "members_count", None)
            if members_count is None:
                try:
                    members_count = await client.get_chat_members_count(chat_id)
                except Exception:  # noqa: BLE001 — not all peers expose a count
                    members_count = None
            return build_chat_info(chat, members_count=members_count)

        return action

    async def post_process(db, redis, account, payload, result):
        if not result or redis is None:
            return
        username = payload.get("username") or result.get("username")
        if username:
            await rc.chat_info_cache_set(redis, str(username), result)

    return await run_task(
        ctx, task_id, builder, post_process=post_process, read_action="get_chat_info"
    )
