"""get_similar_channels — channels Telegram recommends next to a given channel.

The discovery counterpart to the address-based reads: get_chat_info and friends can
only describe a chat we already hold an address for, while this action returns NEW
addresses (Radar's look-alike expansion). Read-only, warmup-exempt, read-budget
limited (§5).
"""
from app.workers import _tg_errors as tg
from app.workers._read_helpers import chat_type_str, not_found_reason
from app.workers.base_task import run_task


def _build_candidate(chat) -> dict:
    # A recommendation without a username stays in the list with an honest None:
    # deciding what to do with an unaddressable candidate is Radar's job, not the
    # worker's. members is None when Telegram didn't send the count flag (min
    # channel objects) — distinct from a real 0 (an actually empty channel).
    return {
        "peer_id": getattr(chat, "id", None),
        "username": getattr(chat, "username", None),
        "title": getattr(chat, "title", None),
        "members": getattr(chat, "members_count", None),
        "type": chat_type_str(getattr(chat, "type", None)),
    }


async def get_similar_channels(ctx, task_id: int) -> dict:
    def builder(payload):
        async def action(client):
            chat_id = payload.get("username") or payload.get("peer_id")
            if not chat_id:
                return {"found": False, "reason": "no_target"}
            try:
                similar = await client.get_similar_channels(chat_id)
            except tg.NOT_FOUND_ERRORS as e:
                return {"found": False, "reason": not_found_reason(e)}
            except ValueError:
                # kurigram raises this when the peer resolves to a user or basic
                # group: the address itself is fine, channel recommendations
                # just don't exist for it. Not a Telegram failure.
                return {"found": False, "reason": "not_a_channel"}

            # get_similar_channels returns None for "no recommendations" rather
            # than an empty list; an empty result is still a resolved target.
            channels = [_build_candidate(c) for c in (similar or [])]
            return {"found": True, "count": len(channels), "channels": channels}

        return action

    return await run_task(ctx, task_id, builder, read_action="get_similar_channels")
