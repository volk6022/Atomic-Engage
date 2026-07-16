"""Warmup DRIVER TDD suite (Phase 7 / US6 — the missing piece that actually RUNS warmup).

The warmup *logic* (schedules, get_allowed_actions, advance_tier_if_due, cross-pairs)
already existed but nothing drove it: no code incremented warmup_day over time, called
tier advancement on a cadence, or enqueued the daily warmup action. `run_warmup_tick`
is that driver — a periodic tick (wired as an ARQ cron) that, per account in warmup:
advances warmup_day from elapsed time, promotes the tier when due, and enqueues exactly
one warmup_action per account per day (deduped in Redis).

Real DB + Redis-shaped fake; skips honestly when Postgres is absent.
"""
from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.core.constants import AccountStatus, TaskStatus, WarmupTier
from app.db.models import Account, Task


def _all_post_ok(mock):
    mock.route().mock(return_value=Response(200))


async def _backdate_created(session_maker, account_id, days):
    async with session_maker() as s:
        async with s.begin():
            acc = (await s.execute(select(Account).where(Account.id == account_id))).scalar_one()
            acc.created_at = datetime.now(timezone.utc) - timedelta(days=days)


@pytest.mark.asyncio
async def test_warmup_tick_enqueues_one_fresh_action_then_dedupes(
    account_factory, session_maker, fake_redis
):
    from app.services import warmup

    ids = await account_factory(status="warmup", warmup_tier="fresh", use_case="reactions")
    acc = ids["account_id"]

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        r1 = await warmup.run_warmup_tick(session_maker, fake_redis)
        r2 = await warmup.run_warmup_tick(session_maker, fake_redis)  # same day -> dedupe

    assert acc in r1["enqueued"]
    assert acc not in r2["enqueued"]

    # exactly one warmup_action task was created and enqueued for this account
    async with session_maker() as s:
        tasks = (
            await s.execute(
                select(Task).where(Task.account_id == acc, Task.task_type == "warmup_action")
            )
        ).scalars().all()
    assert len(tasks) == 1
    assert tasks[0].payload.get("action") in {"profile_setup", "subscribe_channels", "read"}
    assert any(j[0] == "warmup_action" for j in fake_redis.jobs)


@pytest.mark.asyncio
async def test_warmup_tick_advances_tier_on_elapsed_days(
    account_factory, session_maker, fake_redis
):
    from app.services import warmup

    # cold_dm: fresh=5 + basic=8 -> a basic account at day >=13 advances to intermediate
    ids = await account_factory(status="warmup", warmup_tier="basic", use_case="cold_dm")
    acc = ids["account_id"]
    await _backdate_created(session_maker, acc, days=15)

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        result = await warmup.run_warmup_tick(session_maker, fake_redis)

    assert any(a == acc and t == WarmupTier.INTERMEDIATE for a, t in result["advanced"])
    async with session_maker() as s:
        updated = (await s.execute(select(Account).where(Account.id == acc))).scalar_one()
    assert updated.warmup_tier == WarmupTier.INTERMEDIATE
    assert updated.warmup_day >= 13


@pytest.mark.asyncio
async def test_warmup_tick_reaches_ready_flips_status_active(
    account_factory, session_maker, fake_redis
):
    from app.services import warmup

    # reactions: fresh1+basic2+intermediate2 = 5 cumulative -> intermediate@day>=5 ready
    ids = await account_factory(status="warmup", warmup_tier="intermediate", use_case="reactions")
    acc = ids["account_id"]
    await _backdate_created(session_maker, acc, days=9)

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        await warmup.run_warmup_tick(session_maker, fake_redis)

    async with session_maker() as s:
        updated = (await s.execute(select(Account).where(Account.id == acc))).scalar_one()
    assert updated.warmup_tier == WarmupTier.READY
    assert updated.status == AccountStatus.ACTIVE


@pytest.mark.asyncio
async def test_warmup_tick_skips_non_warmup_accounts(
    account_factory, session_maker, fake_redis
):
    from app.services import warmup

    active = (await account_factory(status="active", warmup_tier="ready"))["account_id"]
    banned = (await account_factory(status="banned", warmup_tier="fresh"))["account_id"]

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        result = await warmup.run_warmup_tick(session_maker, fake_redis)

    assert active not in result["enqueued"]
    assert banned not in result["enqueued"]
    async with session_maker() as s:
        n = (
            await s.execute(
                select(Task).where(Task.task_type == "warmup_action", Task.account_id.in_([active, banned]))
            )
        ).scalars().all()
    assert n == []
