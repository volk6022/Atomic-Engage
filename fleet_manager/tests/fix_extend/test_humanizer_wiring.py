"""The humanizer is actually applied in the worker path before the Telegram call
(FR-330-333). HUMANIZE_ACTIONS is off in the test env, so these flip it on and spy
the Humanizer — proving the wiring without sleeping the real inter-action floor.
"""
import uuid

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.core.config import get_settings
from app.core.constants import TaskStatus
from app.db.models import Task
import app.services.humanizer as humanizer_mod
from app.workers.react import react
from app.workers.send_message import send_message


class _SpyHumanizer:
    """Records which human delay was requested instead of sleeping."""

    calls: list = []

    def __init__(self, *a, **k):
        pass

    async def typing_delay(self, text):
        _SpyHumanizer.calls.append(("typing", text))
        return None

    async def reaction_delay(self):
        _SpyHumanizer.calls.append(("reaction", None))

    async def inter_action_delay(self):
        _SpyHumanizer.calls.append(("inter_action", None))


@pytest.fixture
def spy_humanizer(monkeypatch):
    _SpyHumanizer.calls = []
    monkeypatch.setattr(get_settings(), "HUMANIZE_ACTIONS", True)
    monkeypatch.setattr(humanizer_mod, "Humanizer", _SpyHumanizer)
    return _SpyHumanizer


async def _mk_task(session_maker, account_id, *, task_type, payload):
    async with session_maker() as s:
        async with s.begin():
            t = Task(
                external_id=uuid.uuid4().hex,
                account_id=account_id,
                task_type=task_type,
                payload=payload,
                status=TaskStatus.QUEUED,
                webhook_url="https://hook.test/result",
                priority=5,
            )
            s.add(t)
            await s.flush()
            return t.id


def _ok(mock):
    mock.route().mock(return_value=Response(200))


@pytest.mark.asyncio
async def test_send_message_types_before_sending(
    account_factory, session_maker, fake_tg, fake_redis, spy_humanizer
):
    ids = await account_factory(status="active", warmup_tier="ready", use_case="cold_dm")
    tid = await _mk_task(
        session_maker, ids["account_id"],
        task_type="send_message",
        payload={"peer_id": 1, "recipient_username": "bob", "text": "hello world"},
    )

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        result = await send_message(ctx, tid)

    assert ("typing", "hello world") in spy_humanizer.calls   # typed the message
    assert result.get("telegram_message_id") == 123           # and still sent


@pytest.mark.asyncio
async def test_react_uses_reaction_delay(
    account_factory, session_maker, fake_tg, fake_redis, spy_humanizer
):
    ids = await account_factory(status="active", warmup_tier="ready", use_case="reactions")
    tid = await _mk_task(
        session_maker, ids["account_id"],
        task_type="react",
        payload={"peer_id": 5, "message_id": 9, "reaction": "👍"},
    )

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        await react(ctx, tid)

    assert ("reaction", None) in spy_humanizer.calls


@pytest.mark.asyncio
async def test_humanize_skipped_when_flag_off(
    account_factory, session_maker, fake_tg, fake_redis, monkeypatch
):
    """With HUMANIZE_ACTIONS off (the test default), no humanizer delay is requested."""
    _SpyHumanizer.calls = []
    monkeypatch.setattr(get_settings(), "HUMANIZE_ACTIONS", False)
    monkeypatch.setattr(humanizer_mod, "Humanizer", _SpyHumanizer)

    ids = await account_factory(status="active", warmup_tier="ready", use_case="cold_dm")
    tid = await _mk_task(
        session_maker, ids["account_id"],
        task_type="send_message",
        payload={"peer_id": 1, "recipient_username": "bob", "text": "hi"},
    )

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        await send_message(ctx, tid)

    assert _SpyHumanizer.calls == []
