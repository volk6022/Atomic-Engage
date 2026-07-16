"""TelemetryEvent — the research instrument (FR-143). Real DB, faked kurigram.

Proves the store is actually WRITTEN by the runtime (not just that the model exists):
a successful action, a ban (closing the survival window), a budget-defer, and
onboarding each leave the right behavioural-shape row — with no message content.
"""
import uuid

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.db.models import Account, TelemetryEvent
from app.services import budget, telemetry
from app.workers.send_message import send_message

API_KEY = "change_me_in_production"
H = {"X-API-Key": API_KEY}


async def _mk_task(session_maker, account_id, *, task_type="send_message", payload=None):
    from app.core.constants import TaskStatus
    from app.db.models import Task

    async with session_maker() as s:
        async with s.begin():
            t = Task(
                external_id=uuid.uuid4().hex,
                account_id=account_id,
                task_type=task_type,
                payload=payload or {"peer_id": 1, "recipient_username": "bob", "text": "hi"},
                status=TaskStatus.QUEUED,
                webhook_url="https://hook.test/result",
                priority=5,
            )
            s.add(t)
            await s.flush()
            return t.id


def _ok(mock):
    mock.route().mock(return_value=Response(200))


async def _events(session_maker, account_id):
    async with session_maker() as s:
        return (
            await s.execute(
                select(TelemetryEvent).where(TelemetryEvent.account_id == account_id)
            )
        ).scalars().all()


# --- the model exists (the old pending marker, now real) --------------------------
def test_telemetry_event_model_importable():
    from app.db import models

    assert hasattr(models, "TelemetryEvent")
    assert models.TelemetryEvent.__tablename__ == "telemetry_events"


# --- a successful send writes an `action`/ok row with the params in force ----------
@pytest.mark.asyncio
async def test_send_records_action_event(account_factory, session_maker, fake_tg, fake_redis):
    ids = await account_factory(status="active", warmup_tier="ready", use_case="cold_dm")
    acc = ids["account_id"]
    tid = await _mk_task(session_maker, acc)

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        await send_message(ctx, tid)

    evs = await _events(session_maker, acc)
    actions = [e for e in evs if e.event_type == telemetry.ACTION]
    assert len(actions) == 1
    e = actions[0]
    assert e.action_type == "send_message"
    assert e.target_kind == "user"
    assert e.outcome == "ok"
    assert e.warmup_params == {"use_case": "cold_dm", "warmup_tier": "ready"}


# --- a ban writes a `banned` row AND closes the survival window (banned_at) --------
@pytest.mark.asyncio
async def test_ban_records_event_and_sets_banned_at(
    account_factory, session_maker, fake_tg, fake_redis
):
    from pyrogram.errors import UserDeactivated

    ids = await account_factory(status="active", warmup_tier="ready", use_case="cold_dm")
    acc = ids["account_id"]
    tid = await _mk_task(session_maker, acc)
    fake_tg["raise"] = UserDeactivated()

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        await send_message(ctx, tid)

    evs = await _events(session_maker, acc)
    banned = [e for e in evs if e.event_type == telemetry.BANNED]
    assert len(banned) == 1
    assert banned[0].outcome == "banned"
    assert banned[0].cause == "UserDeactivated"
    async with session_maker() as s:
        account = (await s.execute(select(Account).where(Account.id == acc))).scalar_one()
    assert account.banned_at is not None        # survival window closed


# --- a budget-defer writes an `action`/deferred row with the binding cause ---------
@pytest.mark.asyncio
async def test_budget_defer_records_deferred_event(
    account_factory, session_maker, fake_tg, fake_redis
):
    ids = await account_factory(status="active", warmup_tier="ready", use_case="cold_dm")
    acc = ids["account_id"]
    cap = budget.effective_cap("conservative", "cold_dm", "messages_per_day", False)
    fake_redis.kv[f"rate:budget:acct:{acc}:messages_per_day"] = str(cap)
    tid = await _mk_task(session_maker, acc)

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        await send_message(ctx, tid)

    evs = await _events(session_maker, acc)
    deferred = [e for e in evs if e.outcome == "deferred"]
    assert len(deferred) == 1
    assert deferred[0].event_type == telemetry.ACTION
    assert deferred[0].cause == "budget_per_account"


# --- onboarding opens the survival window + writes an `onboarded` row --------------
@pytest.mark.asyncio
async def test_onboarding_records_onboarded_and_first_seen(async_client, session_maker):
    import random

    body = {
        "phone": f"+1346{random.randint(2, 9)}{random.randint(0, 999999):06d}",
        "session_string": "s",
        "proxy_url": "socks5://u__cr.us:p@np.example.com:11000",
        "use_case": "reactions",
        "proxy_country": "US",
        "proxy_type": "residential",
        "api_id": 2040,
        "api_hash": "b18441a1ff607e10a989891a5462e627",
        "cohort": "cohortA",
    }
    r = await async_client.post("/v1/accounts/", json=body, headers=H)
    assert r.status_code == 201, r.text
    acc = r.json()["account_id"]

    async with session_maker() as s:
        account = (await s.execute(select(Account).where(Account.id == acc))).scalar_one()
    assert account.cohort == "cohortA"
    assert account.first_seen_at is not None       # survival window opened

    evs = await _events(session_maker, acc)
    onboarded = [e for e in evs if e.event_type == telemetry.ONBOARDED]
    assert len(onboarded) == 1
    assert onboarded[0].cohort == "cohortA"


# --- the recorder is a thin atomic append (no PII fields, queryable) ---------------
@pytest.mark.asyncio
async def test_record_appends_and_is_queryable(account_factory, session_maker):
    ids = await account_factory(status="active")
    acc = ids["account_id"]
    async with session_maker() as s:
        await telemetry.record(
            s, event_type=telemetry.SURVIVAL_TICK, account_id=acc, outcome="ok", flush=True
        )
        await s.commit()
    evs = await _events(session_maker, acc)
    assert any(e.event_type == telemetry.SURVIVAL_TICK for e in evs)
