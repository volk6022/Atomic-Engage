"""Pure builder for the `incoming_message` webhook payload (Feature 005).

Kept side-effect-free (no DB/Redis) so it is unit-testable in isolation, mirroring
`app.workers._read_helpers.build_chat_info`. The Watcher's `UpdateHandler` calls this to
shape every forwarded update — crucially handling channel/broadcast posts, where
`message.from_user is None` and the source is the `sender_chat` / `chat` instead.
"""
from __future__ import annotations

from typing import Optional


def _chat_type_str(chat_type) -> Optional[str]:
    """Normalise a kurigram ChatType enum to a lowercase string (channel/supergroup/…)."""
    if chat_type is None:
        return None
    return getattr(chat_type, "value", None) or str(chat_type).split(".")[-1].lower()


def _text_of(message) -> str:
    return getattr(message, "text", None) or getattr(message, "caption", None) or ""


def _channel_origin(message) -> tuple[Optional[int], Optional[int]]:
    """The channel post this message was auto-relayed from: `(chat_id, message_id)`.

    kurigram exposes this as `forward_origin` (a `MessageOrigin` object), not as a flat
    `forward_from_message_id` — reading the flat name yields None forever.

    Only the CHANNEL origin is of interest. A comment in a linked discussion group can
    be linked to on its own, but that link opens the group; without the id of the post
    in the channel there is no way to offer "open the post this comments on", and a
    person who is not a member of the group has nowhere useful to land.
    """
    origin = getattr(message, "forward_origin", None)
    kind = getattr(getattr(origin, "type", None), "value", None)
    if origin is None or kind != "channel":
        return None, None
    return (getattr(getattr(origin, "chat", None), "id", None),
            getattr(origin, "message_id", None))


def build_incoming_message_payload(account_id: int, message) -> dict:
    """Map an incoming `Message` to the `incoming_message` webhook body.

    `from_peer_id` resolution (backward-compatible with the user-DM path): the sending
    user's id when present, else the anonymous `sender_chat` id, else the chat id. This
    lets channel broadcast posts (from_user=None) flow through instead of crashing.
    """
    from_user = getattr(message, "from_user", None)
    sender_chat = getattr(message, "sender_chat", None)
    chat = getattr(message, "chat", None)

    chat_id = getattr(chat, "id", None)
    chat_type = _chat_type_str(getattr(chat, "type", None))

    if from_user is not None:
        from_peer_id = getattr(from_user, "id", None)
    elif sender_chat is not None:
        from_peer_id = getattr(sender_chat, "id", None)
    else:
        from_peer_id = chat_id

    date = getattr(message, "date", None)

    # Thread root: `message_thread_id` is populated for forums only, while a comment in
    # a (non-forum) linked discussion group carries `reply_to_top_message_id` instead.
    # Same precedence as `app.workers._read_helpers.build_post` — history and realtime
    # land in one table downstream, so the two paths must agree field for field.
    message_thread_id = (
        getattr(message, "message_thread_id", None)
        or getattr(message, "reply_to_top_message_id", None)
    )

    forward_from_chat_id, forward_from_message_id = _channel_origin(message)

    return {
        "event": "incoming_message",
        "account_id": account_id,
        "from_peer_id": from_peer_id,
        "chat_id": chat_id,
        "message": _text_of(message),
        "message_id": getattr(message, "id", None),
        "date": date.isoformat() if date is not None else None,
        # Feature 005 enrichment — lets n8n route/prioritise by source channel.
        "chat_username": getattr(chat, "username", None),
        "chat_title": getattr(chat, "title", None),
        "chat_type": chat_type,
        "is_channel_post": chat_type == "channel",
        "sender_username": getattr(from_user, "username", None),
        "sender_chat_id": getattr(sender_chat, "id", None),
        # Thread/author enrichment: a lead-gen consumer has to tell a human's comment
        # apart from the linked channel's post auto-relayed into the discussion group,
        # and has to know which thread it belongs to. kurigram spells the forward flag
        # `automatic_forward`, with no `is_` prefix — reading the prefixed name yields
        # False forever and silently erases the distinction.
        "from_first_name": getattr(from_user, "first_name", None),
        "from_last_name": getattr(from_user, "last_name", None),
        "from_is_bot": bool(getattr(from_user, "is_bot", False) or False),
        "reply_to_message_id": getattr(message, "reply_to_message_id", None),
        "message_thread_id": message_thread_id,
        "is_automatic_forward": bool(getattr(message, "automatic_forward", False) or False),
        # The channel post behind an auto-relayed thread root. Mirrored field for field
        # in `app.workers._read_helpers.build_post`: history and realtime land in one
        # table downstream, and a field present on only one path is a field the
        # consumer cannot rely on.
        "forward_from_chat_id": forward_from_chat_id,
        "forward_from_message_id": forward_from_message_id,
    }
