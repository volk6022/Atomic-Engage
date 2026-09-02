"""Shared mappers for read-only research actions (§3.1–§3.3).

Turn kurigram `Chat`/`Message` objects into the plain JSON result shapes the
research agent consumes, keeping the per-worker code thin.
"""
from __future__ import annotations

from typing import Optional

from app.workers._extract import extract_contacts


def verif_flag(obj, name: str) -> bool:
    """Read a verification flag (is_verified/is_scam/is_fake) preferring the new
    `verification_status` object, falling back to the deprecated top-level attr (kept
    for plain test doubles). Avoids kurigram's deprecation warning on real objects."""
    vs = getattr(obj, "verification_status", None)
    if vs is not None:
        return bool(getattr(vs, name, False) or False)
    return bool(getattr(obj, name, False) or False)


def chat_type_str(chat_type) -> Optional[str]:
    """Normalise a kurigram ChatType enum to a lowercase string (channel/supergroup/…)."""
    if chat_type is None:
        return None
    return getattr(chat_type, "value", None) or str(chat_type).split(".")[-1].lower()


def _text_of(message) -> Optional[str]:
    if message is None:
        return None
    return getattr(message, "text", None) or getattr(message, "caption", None)


# Maps a NOT_FOUND_ERRORS exception to a stable machine-readable reason (§6 contract):
# invalid username vs. a real but unreadable private channel vs. a stale peer are
# different situations for the Radar channel registry, even though all three used to
# collapse into the same bare `null`.
_NOT_FOUND_REASONS = {
    "UsernameNotOccupied": "username_not_found",
    "UsernameInvalid": "username_not_found",
    "ChannelPrivate": "not_public",
    "ChannelInvalid": "not_public",
    "PeerIdInvalid": "peer_unknown",
}


def not_found_reason(exc: BaseException) -> str:
    """Classify one of `_tg_errors.NOT_FOUND_ERRORS` into a §6 `reason` string."""
    return _NOT_FOUND_REASONS.get(type(exc).__name__, "not_found")


def build_chat_info(chat, *, members_count: Optional[int] = None) -> dict:
    """Map a `Chat` to the get_chat_info result (§3.2)."""
    description = getattr(chat, "description", None)
    bio = getattr(chat, "bio", None)
    pinned = _text_of(getattr(chat, "pinned_message", None))
    linked = getattr(chat, "linked_chat", None)

    mc = members_count if members_count is not None else getattr(chat, "members_count", None)

    return {
        "peer_id": getattr(chat, "id", None),
        "type": chat_type_str(getattr(chat, "type", None)),
        "title": getattr(chat, "title", None),
        "username": getattr(chat, "username", None),
        "description": description,
        "members_count": mc,
        "is_verified": verif_flag(chat, "is_verified"),
        "is_scam": verif_flag(chat, "is_scam"),
        "linked_chat_username": getattr(linked, "username", None) if linked else None,
        "pinned_message_text": pinned,
        "extracted": extract_contacts(description, bio, pinned),
    }


def _channel_origin(message) -> tuple[Optional[int], Optional[int]]:
    """The channel post this message was auto-relayed from: `(chat_id, message_id)`.

    kurigram exposes this as `forward_origin` (a `MessageOrigin` object), not as a flat
    `forward_from_message_id` — reading the flat name yields None forever.

    Mirrors `app.watchers._message_payload._channel_origin` deliberately: history and
    realtime land in one table downstream, and a field present on only one path is a
    field the consumer cannot rely on.
    """
    origin = getattr(message, "forward_origin", None)
    kind = getattr(getattr(origin, "type", None), "value", None)
    if origin is None or kind != "channel":
        return None, None
    return (getattr(getattr(origin, "chat", None), "id", None),
            getattr(origin, "message_id", None))


def build_post(message) -> dict:
    """Map a `Message` to the post shape shared by get_chat_history (§3.3, §1 contract).

    Author/thread fields mirror `app.watchers._message_payload.build_incoming_message_payload`:
    channel posts and anonymous admins carry `message.from_user is None`, so the human
    author is read defensively via getattr and `sender_chat` is surfaced alongside it
    rather than raising.
    """
    text = _text_of(message)
    extracted = extract_contacts(text)
    date = getattr(message, "date", None)
    edit_date = getattr(message, "edit_date", None)

    from_user = getattr(message, "from_user", None)
    sender_chat = getattr(message, "sender_chat", None)
    reply_to_message = getattr(message, "reply_to_message", None)

    # Thread root: `message_thread_id` is populated for forum topics only; a comment
    # in a (non-forum) linked discussion group instead carries `reply_to_top_message_id`.
    # Fall back to the replied-to message's own id when kurigram exposes neither header
    # field directly (defensive — normally one of the two is set).
    message_thread_id = (
        getattr(message, "message_thread_id", None)
        or getattr(message, "reply_to_top_message_id", None)
        or getattr(reply_to_message, "id", None)
    )

    forward_from_chat_id, forward_from_message_id = _channel_origin(message)

    return {
        "message_id": getattr(message, "id", None),
        "date": date.isoformat() if date is not None else None,
        "text": text,
        "views": getattr(message, "views", None),
        "has_media": getattr(message, "media", None) is not None,
        "urls": extracted["urls"],
        "emails": extracted["emails"],
        "phones": extracted["phones"],
        "from_user_id": getattr(from_user, "id", None),
        "from_username": getattr(from_user, "username", None),
        "from_first_name": getattr(from_user, "first_name", None),
        "from_last_name": getattr(from_user, "last_name", None),
        "from_is_bot": bool(getattr(from_user, "is_bot", False) or False),
        "sender_chat_id": getattr(sender_chat, "id", None),
        "sender_chat_username": getattr(sender_chat, "username", None),
        # automatic_forward (kurigram's actual attr, no `is_` prefix): a connected
        # channel's post auto-relayed into its discussion group — the thread root,
        # never a human comment (§1 contract note).
        "is_automatic_forward": bool(getattr(message, "automatic_forward", False) or False),
        "reply_to_message_id": getattr(message, "reply_to_message_id", None),
        "message_thread_id": message_thread_id,
        # The channel post behind an auto-relayed thread root — see `_channel_origin`.
        "forward_from_chat_id": forward_from_chat_id,
        "forward_from_message_id": forward_from_message_id,
        "edit_date": edit_date.isoformat() if edit_date is not None else None,
        "outgoing": bool(getattr(message, "outgoing", False) or False),
    }
