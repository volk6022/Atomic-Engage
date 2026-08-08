"""Registration + pure-mapper coverage for the two new read actions (B5 get_chat_admins,
B6 get_dialogs, §3/§4 contract).

Registration is checked everywhere it must happen so a missed spot fails at import
time here rather than at runtime in production: TaskType, READ_ACTIONS,
base_task.TARGET_KIND, arq_settings.FUNCTIONS, and the /v1/action allowed-action list
(exercised indirectly through create_action, matching test_actions_endpoint.py's
pattern — a 404 "account not found" proves the action passed the 422 allow-list gate).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.constants import READ_ACTIONS, TaskType
from app.workers.base_task import TARGET_KIND
from app.workers.get_chat_admins import _build_admin, _status_str
from app.workers.get_dialogs import _build_dialog


# ── registration ──────────────────────────────────────────────────────────────────

def test_task_type_has_new_actions():
    assert TaskType.GET_CHAT_ADMINS == "get_chat_admins"
    assert TaskType.GET_DIALOGS == "get_dialogs"


def test_read_actions_includes_new_actions():
    assert TaskType.GET_CHAT_ADMINS in READ_ACTIONS
    assert TaskType.GET_DIALOGS in READ_ACTIONS


def test_target_kind_includes_new_actions():
    assert "get_chat_admins" in TARGET_KIND
    assert "get_dialogs" in TARGET_KIND


def test_arq_settings_registers_new_workers():
    from app.workers import arq_settings

    names = {fn.__name__ for fn in arq_settings.FUNCTIONS}
    assert {"get_chat_admins", "get_dialogs"} <= names


@pytest.mark.asyncio
@pytest.mark.parametrize("action,payload", [
    ("get_chat_admins", {"username": "acme"}),
    ("get_dialogs", {"limit": 50}),
])
async def test_action_endpoint_accepts_new_actions(action, payload):
    """A 404 (account not found) proves the action cleared the 422 allow-list check
    in create_action — a missing registration there would 422 before ever reaching
    the account lookup."""
    from fastapi import HTTPException

    from app.api.v1.actions import ActionRequest, create_action

    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute.return_value = result

    req = ActionRequest(
        account_id=1, action=action, payload=payload,
        webhook_url="https://n8n.example.com/webhook/result", priority=5,
    )
    with pytest.raises(HTTPException) as exc_info:
        await create_action(request=req, db=db, api_key="key")

    assert exc_info.value.status_code == 404


# ── get_chat_admins pure mappers ────────────────────────────────────────────────

def test_status_str_owner_maps_to_creator():
    status = SimpleNamespace(value="owner")
    assert _status_str(status) == "creator"


def test_status_str_administrator_maps_to_administrator():
    status = SimpleNamespace(value="administrator")
    assert _status_str(status) == "administrator"


def test_build_admin_maps_all_fields():
    user = SimpleNamespace(id=101, username="mod1", first_name="Mod", is_bot=False)
    member = SimpleNamespace(user=user, status=SimpleNamespace(value="administrator"),
                              custom_title="Head Mod")

    admin = _build_admin(member)

    assert admin == {
        "user_id": 101,
        "username": "mod1",
        "first_name": "Mod",
        "status": "administrator",
        "is_bot": False,
        "custom_title": "Head Mod",
    }


# ── get_dialogs pure mapper ─────────────────────────────────────────────────────

def test_build_dialog_maps_chat_fields():
    chat = SimpleNamespace(id=-100555, type=SimpleNamespace(value="supergroup"),
                            title="Dev Chat", username="devchat")
    dialog = SimpleNamespace(chat=chat, unread_messages_count=3)

    d = _build_dialog(dialog)

    assert d == {
        "peer_id": -100555,
        "type": "supergroup",
        "title": "Dev Chat",
        "username": "devchat",
        "unread_count": 3,
    }
