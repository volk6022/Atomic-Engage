"""P1 read-action TDD suite (docs/research-agent-actions.md §3–§4, §8 P1).

Read-only enrichment lookups for the research agent: resolve_username (enriched),
get_chat_info, get_chat_history. These are warmup-EXEMPT (no behavioural footprint)
but read-budget LIMITED, and chat info is cached. Tests use the faked kurigram
boundary + real DB (skip honestly when Postgres is absent), mirroring
test_worker_runtime.py.
"""
import uuid

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.core.constants import TaskStatus
from app.db.models import Task


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


def _all_post_ok(mock):
    mock.route().mock(return_value=Response(200))


def _ctx(session_maker, fake_redis):
    return {"session_maker": session_maker, "redis": fake_redis}


# ---------------------------------------------------------------------------
# Extraction helper (urls / emails / phones sweep) — pure, no I/O
# ---------------------------------------------------------------------------
def test_extract_contacts_finds_url_email_phone():
    from app.workers._extract import extract_contacts

    out = extract_contacts(
        "About ACME. Reach us at hi@acme.ru or https://acme.ru tel +79991234567"
    )
    assert "hi@acme.ru" in out["emails"]
    assert any("acme.ru" in u for u in out["urls"])
    assert any("79991234567" in p.replace(" ", "") for p in out["phones"])


def test_extract_contacts_empty_on_none():
    from app.workers._extract import extract_contacts

    out = extract_contacts(None)
    assert out == {"urls": [], "emails": [], "phones": []}


# ---------------------------------------------------------------------------
# resolve_username ENRICH (§3.1): add type/title/flags to the result
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resolve_username_result_enriched(
    account_factory, session_maker, fake_tg, fake_redis
):
    from app.workers.resolve_username import resolve_username

    acc = (await account_factory(status="active"))["account_id"]
    tid = await _mk_task(
        session_maker, acc, task_type="resolve_username", payload={"username": "alice"}
    )
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        result = await resolve_username(_ctx(session_maker, fake_redis), tid)

    assert result["peer_id"] == 42
    assert result["type"] in ("user", "bot")
    assert "title" in result
    assert result["is_verified"] is False
    assert result["is_scam"] is False


@pytest.mark.asyncio
async def test_resolve_username_channel_falls_back_to_get_chat(
    account_factory, session_maker, fake_tg, fake_redis
):
    """When get_users is empty (a channel/group), resolve falls back to get_chat
    to populate type/title (§3.1)."""
    from app.workers.resolve_username import resolve_username

    fake_tg["users"] = []  # not a user -> channel path
    acc = (await account_factory(status="active"))["account_id"]
    tid = await _mk_task(
        session_maker, acc, task_type="resolve_username", payload={"username": "acme"}
    )
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        result = await resolve_username(_ctx(session_maker, fake_redis), tid)

    assert result["type"] == "channel"
    assert result["title"] == "ACME"
    assert result["peer_id"] == 93372553


# ---------------------------------------------------------------------------
# get_chat_info (§3.2)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_chat_info_returns_full_profile_and_extracts(
    account_factory, session_maker, fake_tg, fake_redis
):
    from app.workers.get_chat_info import get_chat_info

    acc = (await account_factory(status="active"))["account_id"]
    tid = await _mk_task(
        session_maker, acc, task_type="get_chat_info",
        payload={"username": "acme", "with_pinned": True},
    )
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        result = await get_chat_info(_ctx(session_maker, fake_redis), tid)

    assert result["peer_id"] == 93372553
    assert result["type"] == "channel"
    assert result["title"] == "ACME"
    assert result["members_count"] == 5821
    assert result["linked_chat_username"] == "acme_chat"
    assert "acme.ru/jobs" in (result["pinned_message_text"] or "")
    assert "hi@acme.ru" in result["extracted"]["emails"]

    async with session_maker() as s:
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert task.status == TaskStatus.COMPLETE


@pytest.mark.asyncio
async def test_get_chat_info_caches_result(
    account_factory, session_maker, fake_tg, fake_redis
):
    from app.workers.get_chat_info import get_chat_info

    acc = (await account_factory(status="active"))["account_id"]
    tid = await _mk_task(
        session_maker, acc, task_type="get_chat_info", payload={"username": "acme"}
    )
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        await get_chat_info(_ctx(session_maker, fake_redis), tid)

    # chat_info cache key written for the username
    assert any("acme" in k for k in fake_redis.kv)


# ---------------------------------------------------------------------------
# get_chat_history (§3.3)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_chat_history_returns_posts_with_contacts(
    account_factory, session_maker, fake_tg, fake_redis
):
    from app.workers.get_chat_history import get_chat_history

    acc = (await account_factory(status="active"))["account_id"]
    tid = await _mk_task(
        session_maker, acc, task_type="get_chat_history",
        payload={"username": "acme", "limit": 30},
    )
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        result = await get_chat_history(_ctx(session_maker, fake_redis), tid)

    assert result["count"] == 2
    first = result["posts"][0]
    assert first["message_id"] == 412
    assert first["has_media"] is True
    assert "jobs@acme.ru" in first["emails"]
    assert result["newest_date"] >= result["oldest_date"]


@pytest.mark.asyncio
async def test_get_chat_history_min_id_cursor_filters(
    account_factory, session_maker, fake_tg, fake_redis
):
    """min_id cursor returns only newer posts (incremental poll, §9.2.1)."""
    from app.workers.get_chat_history import get_chat_history

    acc = (await account_factory(status="active"))["account_id"]
    tid = await _mk_task(
        session_maker, acc, task_type="get_chat_history",
        payload={"username": "acme", "limit": 30, "min_id": 411},
    )
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        result = await get_chat_history(_ctx(session_maker, fake_redis), tid)

    assert result["count"] == 1
    assert result["posts"][0]["message_id"] == 412


# ---------------------------------------------------------------------------
# Read budget (§4.1): warmup-exempt but per-account daily-capped; defer on exceed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_chat_info_read_budget_defers_on_exceed(
    account_factory, session_maker, fake_tg, fake_redis, monkeypatch
):
    from app.core import safety_config
    from app.workers.get_chat_info import get_chat_info

    monkeypatch.setattr(safety_config, "get_read_limits", lambda: {"get_chat_info": 1})

    acc = (await account_factory(status="active"))["account_id"]
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        tid1 = await _mk_task(
            session_maker, acc, task_type="get_chat_info", payload={"username": "acme"}
        )
        r1 = await get_chat_info(_ctx(session_maker, fake_redis), tid1)
        tid2 = await _mk_task(
            session_maker, acc, task_type="get_chat_info", payload={"username": "acme"}
        )
        r2 = await get_chat_info(_ctx(session_maker, fake_redis), tid2)

    assert r1.get("peer_id") == 93372553  # first within budget
    assert r2.get("rate_limited") is True  # second exceeds budget
    async with session_maker() as s:
        t2 = (await s.execute(select(Task).where(Task.id == tid2))).scalar_one()
    assert t2.status == TaskStatus.DEFERRED


# ---------------------------------------------------------------------------
# READ_ACTIONS warmup exemption (§4.1) — exercised at the API gate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("action,payload", [
    ("get_chat_info", {"username": "acme"}),
    ("get_chat_history", {"username": "acme", "limit": 10}),
])
async def test_read_actions_exempt_from_warmup_gate(action, payload):
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.api.v1.actions import ActionRequest, create_action

    proxy = MagicMock(); proxy.country = "RU"
    account = MagicMock()
    account.id = 1; account.status = "active"; account.phone_country = "RU"
    account.proxy = proxy; account.use_case = "reactions"; account.warmup_tier = "fresh"

    result = MagicMock(); result.scalar_one_or_none.return_value = account
    db = AsyncMock(); db.execute.return_value = result
    db.refresh = AsyncMock(side_effect=lambda t: None)

    req = ActionRequest(
        account_id=1, action=action, payload=payload,
        webhook_url="https://n8n.example.com/webhook/result", priority=5,
    )
    with patch("arq.create_pool", return_value=AsyncMock()):
        resp = await create_action(request=req, db=db, api_key="key")
    assert resp.status == "queued"


def test_read_actions_constant_exposed():
    """A READ_ACTIONS set generalises the single resolve_username exemption (§4.1)."""
    from app.core.constants import READ_ACTIONS

    assert {"resolve_username", "get_chat_info", "get_chat_history"} <= set(READ_ACTIONS)
