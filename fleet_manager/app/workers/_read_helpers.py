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


def build_post(message) -> dict:
    """Map a `Message` to the post shape shared by get_chat_history (§3.3)."""
    text = _text_of(message)
    extracted = extract_contacts(text)
    date = getattr(message, "date", None)
    return {
        "message_id": getattr(message, "id", None),
        "date": date.isoformat() if date is not None else None,
        "text": text,
        "views": getattr(message, "views", None),
        "has_media": getattr(message, "media", None) is not None,
        "urls": extracted["urls"],
        "emails": extracted["emails"],
        "phones": extracted["phones"],
    }
