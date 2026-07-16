from app.db.redis_client import peer_cache_set
from app.services.peer_resolver import PeerResolver
from app.workers import _tg_errors as tg
from app.workers._read_helpers import chat_type_str, verif_flag
from app.workers.base_task import run_task


async def resolve_username(ctx, task_id: int) -> dict:
    def builder(payload):
        async def action(client):
            username = payload["username"]
            # get_users only accepts USER handles; on a channel/group kurigram raises
            # IndexError internally (empty user list) — fall back to get_chat. A genuine
            # not-found/invalid handle is a clean null.
            user = None
            try:
                users = await client.get_users(username)
                if isinstance(users, (list, tuple)):
                    user = users[0] if users else None
                else:
                    user = users
            except tg.NOT_FOUND_ERRORS:
                return None
            except IndexError:
                user = None  # not a user -> channel/group path below
            if user:
                return {
                    "peer_id": user.id,
                    "username": username,
                    "access_hash": int(getattr(user, "access_hash", 0) or 0),
                    "type": "bot" if getattr(user, "is_bot", False) else "user",
                    "title": " ".join(
                        p for p in (getattr(user, "first_name", None),
                                    getattr(user, "last_name", None)) if p
                    ) or None,
                    "is_verified": verif_flag(user, "is_verified"),
                    "is_scam": verif_flag(user, "is_scam"),
                }
            # Not a user — channel/group/supergroup. get_chat populates type/title so
            # the agent can decide whether to call get_chat_info/get_chat_history next.
            try:
                chat = await client.get_chat(username)
            except tg.NOT_FOUND_ERRORS:
                return None
            if not chat:
                return None
            return {
                "peer_id": getattr(chat, "id", None),
                "username": username,
                "access_hash": int(getattr(chat, "access_hash", 0) or 0),
                "type": chat_type_str(getattr(chat, "type", None)),
                "title": getattr(chat, "title", None),
                "is_verified": verif_flag(chat, "is_verified"),
                "is_scam": verif_flag(chat, "is_scam"),
            }

        return action

    async def post_process(db, redis, account, payload, result):
        if not result or result.get("peer_id") is None:
            return
        await PeerResolver().upsert_peer(
            db,
            peer_id=result["peer_id"],
            username=payload["username"],
            access_hash=result.get("access_hash", 0),
            account_id=account.id,
        )
        if redis is not None:
            await peer_cache_set(redis, payload["username"], result["peer_id"])

    return await run_task(ctx, task_id, builder, post_process=post_process)
