"""get_chat_admins — creator + administrators of a chat (§3 contract).

Exists to satisfy a direct client requirement for Radar (TG lead-gen): never write to
a channel's admins or moderators. Without a way to list them, that rule can't be
enforced. Read-only, warmup-exempt, read-budget limited (§5).
"""
from pyrogram import enums

from app.workers import _tg_errors as tg
from app.workers._read_helpers import not_found_reason
from app.workers.base_task import run_task


def _status_str(status) -> str:
    """Normalise a kurigram ChatMemberStatus to the §3 vocabulary.

    kurigram names the chat owner's status "owner"; the contract (matching Telegram
    Bot API's ChatMemberOwner) calls it "creator". ADMINISTRATORS-filtered members are
    always one of these two, so anything else defensively falls back to "administrator".
    """
    val = getattr(status, "value", None) or str(status).split(".")[-1].lower()
    return "creator" if val == "owner" else "administrator"


def _build_admin(member) -> dict:
    user = getattr(member, "user", None)
    return {
        "user_id": getattr(user, "id", None),
        "username": getattr(user, "username", None),
        "first_name": getattr(user, "first_name", None),
        "status": _status_str(getattr(member, "status", None)),
        "is_bot": bool(getattr(user, "is_bot", False) or False),
        "custom_title": getattr(member, "custom_title", None),
    }


async def get_chat_admins(ctx, task_id: int) -> dict:
    def builder(payload):
        async def action(client):
            chat_id = payload.get("username") or payload.get("peer_id")
            try:
                admins = [
                    _build_admin(m)
                    async for m in client.get_chat_members(
                        chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS
                    )
                ]
            except tg.NOT_FOUND_ERRORS as e:
                return {"found": False, "reason": not_found_reason(e)}

            return {"found": True, "count": len(admins), "admins": admins}

        return action

    return await run_task(ctx, task_id, builder, read_action="get_chat_admins")
