"""search_public_chats — global public chat/channel discovery by text query.

Runs raw.functions.contacts.Search: the server-side ranked answer Telegram's own
search box gives. Unlike every earlier read, the input is a string, not an address —
this is what makes the result NEW to the fleet. Read-only, warmup-exempt,
read-budget limited (§5).
"""
from pyrogram import raw, types, utils

from app.workers._read_helpers import chat_type_str
from app.workers.base_task import run_task

_DEFAULT_LIMIT = 20
# kurigram's own search_contacts caps a single contacts.Search invoke at 100 as
# well; the server clamps regardless, so a higher ceiling would only promise
# results that never arrive.
_MAX_LIMIT = 100


def _build_candidate(chat) -> dict:
    # Same candidate shape as get_similar_channels so Radar filters both feeds
    # with one code path. username is None for chats addressed only by invite
    # link; the candidate stays — dropping it is Radar's call.
    return {
        "peer_id": getattr(chat, "id", None),
        "username": getattr(chat, "username", None),
        "title": getattr(chat, "title", None),
        "members": getattr(chat, "members_count", None),
        "type": chat_type_str(getattr(chat, "type", None)),
    }


async def search_public_chats(ctx, task_id: int) -> dict:
    def builder(payload):
        async def action(client):
            query = str(payload.get("query") or "").strip()
            if not query:
                # A blank query would ask Telegram to enumerate arbitrary
                # top chats; refusing locally costs nothing and is honest.
                return {"found": False, "reason": "empty_query"}

            try:
                limit = int(payload.get("limit") or _DEFAULT_LIMIT)
            except (TypeError, ValueError):
                limit = _DEFAULT_LIMIT
            limit = max(1, min(limit, _MAX_LIMIT))

            found = await client.invoke(
                raw.functions.contacts.Search(q=query, limit=limit)
            )

            # Only chats: user hits (PeerUser referencing found.users) are not
            # public-chat candidates and simply miss this map.
            chats = {c.id: c for c in found.chats}
            candidates: list[dict] = []
            channels: list[dict] = []
            groups: list[dict] = []
            seen: set[int] = set()
            # `results` is the server's global relevance ranking. `my_results`
            # (the account's own contacts/member chats) is skipped on purpose:
            # those are addresses the fleet already holds, and this action
            # exists to return new ones.
            for peer in found.results:
                peer_id = utils.get_raw_peer_id(peer)
                if peer_id is None or peer_id in seen:
                    continue
                parsed = types.Chat._parse_chat(client, chats.get(peer_id))
                if parsed is None:
                    continue
                seen.add(peer_id)
                candidate = _build_candidate(parsed)
                candidates.append(candidate)
                # The channels/groups split lets Radar pick its target class
                # without re-deriving it from `type` downstream.
                (channels if candidate["type"] == "channel" else groups).append(
                    candidate
                )

            return {
                "found": True,
                "query": query,
                "count": len(candidates),
                "results": candidates,
                "channels": channels,
                "chats": groups,
            }

        return action

    return await run_task(ctx, task_id, builder, read_action="search_public_chats")
