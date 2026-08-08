"""get_dialogs — chats the account is actually a member of (§4 contract).

Lets Radar build the "account × channel" matrix: which accounts are already sitting
in which channels/groups, so lead-gen can route comment-watching to accounts that are
already members instead of guessing. There was previously no way to ask an account
this at all. Read-only, warmup-exempt, read-budget limited (§5).

`chat_types` filters client-side (Telegram's dialog list API has no server-side type
filter): we keep pulling from kurigram's own paginated stream until `limit` post-filter
matches are collected, so a small `limit` with a narrow `chat_types` still returns a
full page instead of whatever happened to be in the first raw chunk.
"""
from app.workers._read_helpers import chat_type_str
from app.workers.base_task import run_task

_DEFAULT_LIMIT = 200


def _build_dialog(dialog) -> dict:
    chat = getattr(dialog, "chat", None)
    return {
        "peer_id": getattr(chat, "id", None),
        "type": chat_type_str(getattr(chat, "type", None)),
        "title": getattr(chat, "title", None),
        "username": getattr(chat, "username", None),
        "unread_count": getattr(dialog, "unread_messages_count", None),
    }


async def get_dialogs(ctx, task_id: int) -> dict:
    def builder(payload):
        async def action(client):
            limit = int(payload.get("limit") or _DEFAULT_LIMIT)
            chat_types = payload.get("chat_types")
            type_filter = set(chat_types) if chat_types else None

            dialogs: list[dict] = []
            async for d in client.get_dialogs():
                item = _build_dialog(d)
                if type_filter is not None and item["type"] not in type_filter:
                    continue
                dialogs.append(item)
                if len(dialogs) >= limit:
                    break

            return {"count": len(dialogs), "dialogs": dialogs}

        return action

    return await run_task(ctx, task_id, builder, read_action="get_dialogs")
