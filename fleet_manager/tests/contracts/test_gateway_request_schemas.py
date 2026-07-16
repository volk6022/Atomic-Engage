import pytest
from pydantic import ValidationError

from app.api.v1.actions import (
    SendMessagePayload,
    JoinGroupPayload,
    ReactPayload,
    ResolveUsernamePayload,
    InviteToGroupPayload,
)


def test_send_message_payload_valid():
    payload = SendMessagePayload(
        peer_id=123456789, text="Hello", reply_to_message_id=None
    )
    assert payload.peer_id == 123456789
    assert payload.text == "Hello"


def test_send_message_payload_missing_required():
    with pytest.raises(ValidationError):
        SendMessagePayload(text="Hello")


def test_join_group_payload_valid():
    payload = JoinGroupPayload(invite_link="https://t.me/joinchat/test")
    assert payload.invite_link == "https://t.me/joinchat/test"


def test_join_group_payload_missing():
    with pytest.raises(ValidationError):
        JoinGroupPayload()


def test_react_payload_valid():
    payload = ReactPayload(peer_id=123456789, message_id=999, reaction="👍")
    assert payload.reaction == "👍"


def test_resolve_username_payload_valid():
    payload = ResolveUsernamePayload(username="testuser")
    assert payload.username == "testuser"


def test_invite_to_group_payload_valid():
    payload = InviteToGroupPayload(group_username="testgroup", user_peer_id=123456789)
    assert payload.group_username == "testgroup"
