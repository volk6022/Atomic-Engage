"""Phase 2 (US1) genuine worker-runtime tests — real DB, real Redis-shaped fake,
faked kurigram boundary. These prove the dispatch loop actually executes (FR-101/102/104).

They require Postgres (skip honestly otherwise via the parent conftest db_engine).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.core.constants import AccountStatus, TaskStatus
from app.db.models import Task
from app.workers import arq_settings, recovery
from app.workers import _tg_errors as tg
from app.workers.send_message import send_message


async def _mk_task(
    session_maker,
    account_id,
    *,
    task_type="send_message",
    payload=None,
    status=TaskStatus.QUEUED,
    started_at=None,
    deferred_until=None,
):
    async with session_maker() as s:
        async with s.begin():
            t = Task(
                external_id=uuid.uuid4().hex,
                account_id=account_id,
                task_type=task_type,
                payload=payload or {"recipient_username": "bob", "text": "hi"},
                status=status,
                webhook_url="https://hook.test/result",
                priority=5,
                started_at=started_at,
                deferred_until=deferred_until,
            )
            s.add(t)
            await s.flush()
            return t.id


def _all_post_ok(mock):
    mock.route().mock(return_value=Response(200))  # catch-all: never hit the network


# --- FR-101: worker ctx gets a DB session factory --------------------------------
@pytest.mark.asyncio
async def test_worker_ctx_has_db_and_redis(session_maker, fake_redis):
    ctx = {"redis": fake_redis}
    await arq_settings.on_startup(ctx)
    assert "session_maker" in ctx and ctx["session_maker"] is not None


# --- send_message completes end-to-end (M0 core) ---------------------------------
@pytest.mark.asyncio
async def test_send_message_completes_and_webhooks(
    account_factory, session_maker, fake_tg, fake_redis
):
    ids = await account_factory(status="active", warmup_tier="ready")
    tid = await _mk_task(session_maker, ids["account_id"])

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        result = await send_message(ctx, tid)

    assert result.get("telegram_message_id") == 123
    async with session_maker() as s:
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert task.status == TaskStatus.COMPLETE
    assert task.result["telegram_message_id"] == 123


# --- FR-021: per-account FIFO holds the second task ------------------------------
@pytest.mark.asyncio
async def test_fifo_second_task_waits(account_factory, session_maker, fake_tg, fake_redis):
    ids = await account_factory(status="active")
    acc = ids["account_id"]
    # Task A is already executing for this account.
    await _mk_task(
        session_maker, acc, status=TaskStatus.EXECUTING, started_at=datetime.now(timezone.utc)
    )
    tid_b = await _mk_task(session_maker, acc)

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        result = await send_message(ctx, tid_b)

    assert result.get("queued") is True
    async with session_maker() as s:
        task_b = (await s.execute(select(Task).where(Task.id == tid_b))).scalar_one()
    assert task_b.status == TaskStatus.QUEUED


# --- FR-021: claim is atomic — refuses when another task is executing -------------
@pytest.mark.asyncio
async def test_claim_rejects_when_other_executing(account_factory, session_maker):
    from app.workers.base_task import BaseTask

    acc = (await account_factory(status="active"))["account_id"]
    await _mk_task(
        session_maker, acc, status=TaskStatus.EXECUTING, started_at=datetime.now(timezone.utc)
    )
    tid_b = await _mk_task(session_maker, acc)

    async with session_maker() as s:
        task_b = (await s.execute(select(Task).where(Task.id == tid_b))).scalar_one()
        assert await BaseTask._claim_for_execution(s, task_b) is False

    async with session_maker() as s:
        task_b = (await s.execute(select(Task).where(Task.id == tid_b))).scalar_one()
    assert task_b.status == TaskStatus.QUEUED


# --- FR-021: only the head of the per-account queue may claim ---------------------
@pytest.mark.asyncio
async def test_claim_only_head_of_queue(account_factory, session_maker):
    from app.workers.base_task import BaseTask

    acc = (await account_factory(status="active"))["account_id"]
    tid_a = await _mk_task(session_maker, acc)  # older → queue head
    tid_b = await _mk_task(session_maker, acc)  # younger

    # The younger task is NOT the head → it must not claim.
    async with session_maker() as s:
        task_b = (await s.execute(select(Task).where(Task.id == tid_b))).scalar_one()
        assert await BaseTask._claim_for_execution(s, task_b) is False

    # The head claims and becomes EXECUTING.
    async with session_maker() as s:
        task_a = (await s.execute(select(Task).where(Task.id == tid_a))).scalar_one()
        assert await BaseTask._claim_for_execution(s, task_a) is True

    async with session_maker() as s:
        a = (await s.execute(select(Task).where(Task.id == tid_a))).scalar_one()
        b = (await s.execute(select(Task).where(Task.id == tid_b))).scalar_one()
    assert a.status == TaskStatus.EXECUTING
    assert b.status == TaskStatus.QUEUED


# --- FR-340/342: write-action daily cap defers once the per-account budget is hit --
@pytest.mark.asyncio
async def test_write_budget_caps_and_defers(account_factory, session_maker, fake_tg, fake_redis):
    from app.services import budget

    ids = await account_factory(status="active", warmup_tier="ready", use_case="cold_dm")
    acc = ids["account_id"]
    cap = budget.effective_cap("conservative", "cold_dm", "messages_per_day", False)
    assert cap == 20
    # Pre-fill the per-account messages budget to its cap so the next send is over.
    fake_redis.kv[f"rate:budget:acct:{acc}:messages_per_day"] = str(cap)
    tid = await _mk_task(
        session_maker, acc, payload={"peer_id": 1, "recipient_username": "bob", "text": "hi"}
    )

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        result = await send_message(ctx, tid)

    assert result.get("rate_limited") is True
    assert result.get("binding") == "per_account"
    async with session_maker() as s:
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert task.status == TaskStatus.DEFERRED
    assert task.error_code == "BUDGET_PER_ACCOUNT"


# --- FR-340: a write action UNDER cap runs and consumes one budget unit ------------
@pytest.mark.asyncio
async def test_write_budget_consumes_one_unit_when_allowed(
    account_factory, session_maker, fake_tg, fake_redis
):
    ids = await account_factory(status="active", warmup_tier="ready", use_case="cold_dm")
    acc = ids["account_id"]
    tid = await _mk_task(
        session_maker, acc, payload={"peer_id": 1, "recipient_username": "bob", "text": "hi"}
    )

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        result = await send_message(ctx, tid)

    assert result.get("telegram_message_id") == 123        # sent, not deferred
    assert int(fake_redis.kv[f"rate:budget:acct:{acc}:messages_per_day"]) == 1


# --- FR-013: FloodWait defers, no early retry, account -> flood -------------------
@pytest.mark.asyncio
async def test_floodwait_defers(account_factory, session_maker, fake_tg, fake_redis):
    ids = await account_factory(status="active")
    tid = await _mk_task(session_maker, ids["account_id"])
    fake_tg["raise"] = tg.FloodWait(value=300)

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        result = await send_message(ctx, tid)

    assert "flood_until" in result
    async with session_maker() as s:
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
        from app.db.models import Account

        acc = (await s.execute(select(Account).where(Account.id == ids["account_id"]))).scalar_one()
    assert task.status == TaskStatus.DEFERRED
    assert task.deferred_until is not None
    assert acc.status == AccountStatus.FLOOD


# --- FR-012: ban error marks the account banned ----------------------------------
@pytest.mark.asyncio
async def test_ban_marks_banned(account_factory, session_maker, fake_tg, fake_redis):
    from pyrogram.errors import UserDeactivated

    ids = await account_factory(status="active")
    tid = await _mk_task(session_maker, ids["account_id"])
    fake_tg["raise"] = UserDeactivated()

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        result = await send_message(ctx, tid)

    assert result.get("banned") is True
    async with session_maker() as s:
        from app.db.models import Account

        acc = (await s.execute(select(Account).where(Account.id == ids["account_id"]))).scalar_one()
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert acc.status == AccountStatus.BANNED
    assert task.status == TaskStatus.FAILED


# --- each worker type completes through the shared orchestrator ------------------
@pytest.mark.parametrize(
    "task_type, payload, worker_path",
    [
        ("join_group", {"invite_link": "https://t.me/+abc"}, "app.workers.join_group:join_group"),
        ("react", {"peer_id": 5, "message_id": 9, "reaction": "👍"}, "app.workers.react:react"),
        (
            "invite_to_group",
            {"group_username": "grp", "user_peer_id": 7},
            "app.workers.invite_to_group:invite_to_group",
        ),
        ("warmup_action", {"action": "profile_view"}, "app.workers.warmup_action:warmup_action"),
    ],
)
@pytest.mark.asyncio
async def test_each_worker_completes(
    account_factory, session_maker, fake_tg, fake_redis, task_type, payload, worker_path
):
    import importlib

    mod_name, fn_name = worker_path.split(":")
    worker_fn = getattr(importlib.import_module(mod_name), fn_name)

    ids = await account_factory(status="active", warmup_tier="ready")
    tid = await _mk_task(session_maker, ids["account_id"], task_type=task_type, payload=payload)

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        await worker_fn(ctx, tid)

    async with session_maker() as s:
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert task.status == TaskStatus.COMPLETE


# --- resolve_username persists the peer + per-account access hash -----------------
@pytest.mark.asyncio
async def test_resolve_username_persists_peer(account_factory, session_maker, fake_tg, fake_redis):
    from app.workers.resolve_username import resolve_username
    from app.db.models import GlobalPeer, PeerAccessHash

    ids = await account_factory(status="active")
    tid = await _mk_task(
        session_maker, ids["account_id"], task_type="resolve_username", payload={"username": "alice"}
    )

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        result = await resolve_username(ctx, tid)

    assert result["peer_id"] == 42
    async with session_maker() as s:
        gp = (await s.execute(select(GlobalPeer).where(GlobalPeer.peer_id == 42))).scalar_one()
        pah = (
            await s.execute(
                select(PeerAccessHash).where(
                    PeerAccessHash.account_id == ids["account_id"], PeerAccessHash.peer_id == 42
                )
            )
        ).scalar_one()
    assert gp.username == "alice"
    assert pah.access_hash == 99
    assert fake_redis.kv  # username cached


# --- PEER_ID_INVALID deletes the stale hash and re-resolves ----------------------
@pytest.mark.asyncio
async def test_peer_id_invalid_deletes_hash_and_reresolves(
    account_factory, session_maker, fake_tg, fake_redis
):
    from pyrogram.errors import PeerIdInvalid
    from app.db.models import PeerAccessHash

    ids = await account_factory(status="active")
    acc = ids["account_id"]
    # seed a stale access hash for (acc, peer 5)
    async with session_maker() as s:
        async with s.begin():
            s.add(PeerAccessHash(account_id=acc, peer_id=5, access_hash=123))
    tid = await _mk_task(
        session_maker, acc, payload={"peer_id": 5, "recipient_username": "bob", "text": "x"}
    )
    fake_tg["raise"] = PeerIdInvalid()

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        result = await send_message(ctx, tid)

    assert result.get("error") == "peer_id_invalid"
    async with session_maker() as s:
        row = (
            await s.execute(
                select(PeerAccessHash).where(
                    PeerAccessHash.account_id == acc, PeerAccessHash.peer_id == 5
                )
            )
        ).scalar_one_or_none()
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert row is None  # stale hash deleted
    assert task.status == TaskStatus.FAILED and task.error_code == "PEER_ID_INVALID"
    assert any(j[0] == "resolve_username" for j in fake_redis.jobs)  # re-resolve enqueued


# --- generic Telegram/runtime error marks the task failed ------------------------
@pytest.mark.asyncio
async def test_generic_failure_marks_failed(account_factory, session_maker, fake_tg, fake_redis):
    ids = await account_factory(status="active")
    tid = await _mk_task(session_maker, ids["account_id"])
    fake_tg["raise"] = ValueError("boom")

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        result = await send_message(ctx, tid)

    assert "error" in result
    async with session_maker() as s:
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert task.status == TaskStatus.FAILED and task.error_code == "ValueError"


# --- off-hours defers the task (run_task computes deferred_until) -----------------
@pytest.mark.asyncio
async def test_offhours_defers_task(account_factory, session_maker, fake_tg, fake_redis):
    from app.db.models import Account

    ids = await account_factory(status="active")
    # work_start == work_end => always outside the window
    async with session_maker() as s:
        async with s.begin():
            acc = (await s.execute(select(Account).where(Account.id == ids["account_id"]))).scalar_one()
            acc.work_start = 0
            acc.work_end = 0
    tid = await _mk_task(session_maker, ids["account_id"])

    with respx.mock(assert_all_mocked=False, assert_all_called=False) as m:
        _all_post_ok(m)
        ctx = {"session_maker": session_maker, "redis": fake_redis}
        result = await send_message(ctx, tid)

    assert "deferred_until" in result
    async with session_maker() as s:
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert task.status == TaskStatus.DEFERRED


# --- FR-120: startup recovery resets stale executing tasks -----------------------
@pytest.mark.asyncio
async def test_recover_orphaned_executing_resets_to_queued(
    account_factory, session_maker, fake_redis
):
    ids = await account_factory(status="active")
    stale = datetime.now(timezone.utc) - timedelta(seconds=1000)  # > 600 lease
    tid = await _mk_task(
        session_maker, ids["account_id"], status=TaskStatus.EXECUTING, started_at=stale
    )
    # A genuinely-running task (recent) must NOT be reset.
    fresh = await _mk_task(
        session_maker,
        ids["account_id"],
        status=TaskStatus.EXECUTING,
        started_at=datetime.now(timezone.utc),
    )

    async with session_maker() as db:
        recovered = await recovery.recover_orphaned_tasks(db, lease_seconds=600, redis=fake_redis)

    assert tid in recovered and fresh not in recovered
    async with session_maker() as s:
        t = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert t.status == TaskStatus.QUEUED
    assert any(j[1].get("task_id") == tid for j in fake_redis.jobs)


# --- FR-105: deferred scheduler re-enqueues due tasks ----------------------------
@pytest.mark.asyncio
async def test_deferred_scheduler_reenqueues_due(account_factory, session_maker, fake_redis):
    ids = await account_factory(status="active")
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    tid = await _mk_task(
        session_maker, ids["account_id"], status=TaskStatus.DEFERRED, deferred_until=past
    )

    async with session_maker() as db:
        moved = await recovery.reenqueue_due_deferred(db, redis=fake_redis)

    assert tid in moved
    async with session_maker() as s:
        t = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert t.status == TaskStatus.QUEUED
    assert any(j[1].get("task_id") == tid for j in fake_redis.jobs)
