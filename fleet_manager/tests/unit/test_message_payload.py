"""Unit tests for the pure incoming-message payload builder (Feature 005).

Covers channel broadcast posts (from_user is None), user DMs, anonymous group admins,
and missing text — no DB/Redis, matching the `_read_helpers.build_chat_info` pattern.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from app.watchers._message_payload import build_incoming_message_payload


def _chat(**kw):
    kw.setdefault("id", -1001)
    kw.setdefault("type", SimpleNamespace(value="channel"))
    kw.setdefault("username", None)
    kw.setdefault("title", None)
    return SimpleNamespace(**kw)


def test_message_payload_channel_post_from_user_none():
    """A channel broadcast post (from_user=None) must not crash and must be flagged."""
    chat = _chat(id=-1001627075588, type=SimpleNamespace(value="channel"),
                 username="ru_pythonjobs", title="Python Jobs")
    msg = SimpleNamespace(
        from_user=None,
        sender_chat=chat,
        chat=chat,
        id=42,
        text="Backend Python вакансия, удалёнка",
        caption=None,
        date=datetime.now(timezone.utc),
    )
    p = build_incoming_message_payload(7, msg)

    assert p["event"] == "incoming_message"
    assert p["account_id"] == 7
    assert p["is_channel_post"] is True
    assert p["chat_username"] == "ru_pythonjobs"
    assert p["chat_title"] == "Python Jobs"
    assert p["chat_type"] == "channel"
    assert p["message"] == "Backend Python вакансия, удалёнка"
    assert p["message_id"] == 42
    # backward-compat: from_peer_id falls back to the channel/chat id
    assert p["from_peer_id"] == -1001627075588
    assert p["chat_id"] == -1001627075588


def test_message_payload_user_dm_preserves_from_peer_id():
    """A normal user DM keeps from_peer_id = the sending user (no regression)."""
    user = SimpleNamespace(id=555000111, username="alice", first_name="A", last_name=None)
    chat = _chat(id=555000111, type=SimpleNamespace(value="private"),
                 username="alice", title=None)
    msg = SimpleNamespace(
        from_user=user, sender_chat=None, chat=chat, id=9001,
        text="hi", caption=None, date=datetime.now(timezone.utc),
    )
    p = build_incoming_message_payload(7, msg)

    assert p["is_channel_post"] is False
    assert p["from_peer_id"] == 555000111
    assert p["sender_username"] == "alice"
    assert p["message"] == "hi"


def test_message_payload_caption_fallback_and_missing_username():
    """Media posts without text use caption; missing username/title tolerated."""
    chat = _chat(id=-100777, type=SimpleNamespace(value="channel"),
                 username=None, title="No Handle")
    msg = SimpleNamespace(
        from_user=None, sender_chat=chat, chat=chat, id=7,
        text=None, caption="see the picture", date=None,
    )
    p = build_incoming_message_payload(1, msg)

    assert p["message"] == "see the picture"
    assert p["chat_username"] is None
    assert p["chat_title"] == "No Handle"
    assert p["date"] is None


def test_message_payload_anonymous_group_admin_uses_sender_chat():
    """Anonymous group admin: from_user None, sender_chat set, chat is the group."""
    group = _chat(id=-100200, type=SimpleNamespace(value="supergroup"),
                  username="devchat", title="Dev Chat")
    sender = _chat(id=-100999, type=SimpleNamespace(value="channel"),
                   username="editor", title="Editor")
    msg = SimpleNamespace(
        from_user=None, sender_chat=sender, chat=group, id=5,
        text="pinned", caption=None, date=datetime.now(timezone.utc),
    )
    p = build_incoming_message_payload(3, msg)

    assert p["chat_id"] == -100200
    assert p["is_channel_post"] is False  # the CHAT is a supergroup, not a channel
    assert p["sender_chat_id"] == -100999
    assert p["from_peer_id"] == -100999  # sender_chat wins over chat when no user
