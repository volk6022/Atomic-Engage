"""Unit tests for `_read_helpers.build_post` author/thread enrichment (§1 contract).

Pure mapper, no DB/Redis/Telegram — matches the `test_message_payload.py` pattern for
`app.watchers._message_payload.build_incoming_message_payload`, which build_post
mirrors for the get_chat_history read path.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from app.workers._read_helpers import build_post, not_found_reason


def _msg(**kw):
    kw.setdefault("id", 1)
    kw.setdefault("date", datetime.now(timezone.utc))
    kw.setdefault("text", "hello")
    kw.setdefault("caption", None)
    kw.setdefault("views", None)
    kw.setdefault("media", None)
    kw.setdefault("from_user", None)
    kw.setdefault("sender_chat", None)
    kw.setdefault("automatic_forward", None)
    kw.setdefault("reply_to_message_id", None)
    kw.setdefault("reply_to_message", None)
    kw.setdefault("message_thread_id", None)
    kw.setdefault("reply_to_top_message_id", None)
    kw.setdefault("edit_date", None)
    kw.setdefault("outgoing", None)
    return SimpleNamespace(**kw)


def test_build_post_human_author_fields_populated():
    user = SimpleNamespace(id=555, username="alice", first_name="Alice", last_name="A",
                            is_bot=False)
    msg = _msg(from_user=user, outgoing=True)

    p = build_post(msg)

    assert p["from_user_id"] == 555
    assert p["from_username"] == "alice"
    assert p["from_first_name"] == "Alice"
    assert p["from_last_name"] == "A"
    assert p["from_is_bot"] is False
    assert p["sender_chat_id"] is None
    assert p["outgoing"] is True
    # Existing fields untouched (backward compat).
    assert p["message_id"] == 1
    assert p["text"] == "hello"


def test_build_post_channel_post_from_user_none_uses_sender_chat():
    """Posts from a channel/anonymous admin: from_user is None, sender_chat is set.
    Must not raise AttributeError and must surface sender_chat fields instead."""
    sender = SimpleNamespace(id=-1001111, username="acme_channel")
    msg = _msg(from_user=None, sender_chat=sender)

    p = build_post(msg)

    assert p["from_user_id"] is None
    assert p["from_username"] is None
    assert p["from_is_bot"] is False
    assert p["sender_chat_id"] == -1001111
    assert p["sender_chat_username"] == "acme_channel"


def test_build_post_automatic_forward_flagged():
    """A connected-channel post auto-relayed into the discussion group: the real
    kurigram attribute is `automatic_forward` (no `is_` prefix) — must be read as
    such and re-exposed as `is_automatic_forward`."""
    msg = _msg(automatic_forward=True)

    p = build_post(msg)

    assert p["is_automatic_forward"] is True


def test_build_post_automatic_forward_defaults_false():
    msg = _msg(automatic_forward=None)

    p = build_post(msg)

    assert p["is_automatic_forward"] is False


def test_build_post_reply_populates_reply_to_message_id():
    msg = _msg(reply_to_message_id=777)

    p = build_post(msg)

    assert p["reply_to_message_id"] == 777


def test_build_post_thread_root_prefers_reply_to_top_message_id():
    """Non-forum discussion-group comments carry the thread root in
    reply_to_top_message_id, not message_thread_id (that field is forum-only)."""
    msg = _msg(message_thread_id=None, reply_to_top_message_id=900)

    p = build_post(msg)

    assert p["message_thread_id"] == 900


def test_build_post_thread_root_prefers_message_thread_id_when_both_set():
    msg = _msg(message_thread_id=42, reply_to_top_message_id=900)

    p = build_post(msg)

    assert p["message_thread_id"] == 42


def test_build_post_thread_root_falls_back_to_reply_to_message():
    reply = SimpleNamespace(id=123)
    msg = _msg(message_thread_id=None, reply_to_top_message_id=None, reply_to_message=reply)

    p = build_post(msg)

    assert p["message_thread_id"] == 123


def test_build_post_edit_date_iso_or_null():
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    msg = _msg(edit_date=dt)
    p = build_post(msg)
    assert p["edit_date"] == dt.isoformat()

    msg2 = _msg(edit_date=None)
    p2 = build_post(msg2)
    assert p2["edit_date"] is None


# ── not_found_reason (§6 contract) ────────────────────────────────────────────────

def test_not_found_reason_maps_known_types():
    class UsernameNotOccupied(Exception):
        pass

    class ChannelPrivate(Exception):
        pass

    class PeerIdInvalid(Exception):
        pass

    assert not_found_reason(UsernameNotOccupied()) == "username_not_found"
    assert not_found_reason(ChannelPrivate()) == "not_public"
    assert not_found_reason(PeerIdInvalid()) == "peer_unknown"


def test_not_found_reason_falls_back_for_unknown_type():
    class SomeOtherError(Exception):
        pass

    assert not_found_reason(SomeOtherError()) == "not_found"
