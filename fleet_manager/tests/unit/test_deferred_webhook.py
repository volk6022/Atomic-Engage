"""E4: a task deferred on budget must notify its requester (`task_deferred` webhook).

Before this, the two budget branches (read gate and write gate) parked the task with
`status=deferred` + `error_code=…` and told nobody: the caller had said "queued" at
submit time and then heard nothing until (if ever) the rescheduled run completed. The
flood branch already had the cure — one queued envelope via `_webhook`, delivered
asynchronously with retries — so the fix is the same envelope shape at the same point
(commit → webhook → enqueue_next → return) in both budget branches.

The envelope is exactly five fields; `error_code` is read back from `task.error_code`
(single source), and the URL is `task.webhook_url or N8N_SYSTEM_WEBHOOK_URL` — a
customer-supplied webhook_url must hear about *its own* task slipping, the system
webhook is only the fallback (same as flood_wait).

Harness notes: FakeRedis follows tests/unit/test_budget_consume_is_atomic.py:37-100
(both Lua scripts honoured, everything yields control); the DB layer is the real
Postgres fixture — the delivery row in `webhook_deliveries` IS the contract here, and
`_claim_for_execution` uses a pg advisory lock, so an in-memory fake would prove
nothing.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.core.constants import TaskStatus
from app.db.models import ApiCredential, Task, WebhookDelivery

HOOK = "https://hook.test/e4"


class FakeRedis:
    """Minimal Redis with the guarantees the two Lua scripts rely on.

    Same shape as the fixture in test_budget_consume_is_atomic.py: a script runs
    alone (lock around `eval`), the INCR script sets the TTL on the first write, and
    the consume script returns `[0, remaining...]` or `[i]` naming the first counter
    that refused. `enqueue_job` is the arq call `enqueue_next` makes in the branches
    under test.
    """

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttl: dict[str, int] = {}
        self.jobs: list[tuple[str, dict]] = []
        self._lock = asyncio.Lock()

    async def get(self, key):
        await asyncio.sleep(0)
        value = self.store.get(key)
        return None if value is None else str(value)

    async def incr(self, key):
        await asyncio.sleep(0)
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key, ttl):
        await asyncio.sleep(0)
        self.ttl[key] = ttl
        return True

    async def eval(self, script, numkeys, *args):
        async with self._lock:
            return await self._run(script, list(args[:numkeys]), list(args[numkeys:]))

    async def _run(self, script, keys, argv):
        await asyncio.sleep(0)
        if "INCR" in script and len(keys) == 1 and len(argv) == 1:
            self.store[keys[0]] = self.store.get(keys[0], 0) + 1
            if self.store[keys[0]] == 1:
                self.ttl[keys[0]] = int(argv[0])
            return self.store[keys[0]]
        caps = [int(x) for x in argv[:len(keys)]]
        ttl = int(argv[len(keys)])
        for index, (key, cap) in enumerate(zip(keys, caps), start=1):
            if self.store.get(key, 0) + 1 > cap:
                return [index]
        out = [0]
        for key, cap in zip(keys, caps):
            self.store[key] = self.store.get(key, 0) + 1
            if self.store[key] == 1:
                self.ttl[key] = ttl
            out.append(cap - self.store[key])
        return out

    async def enqueue_job(self, func, **kwargs):
        self.jobs.append((func, kwargs))


@pytest.fixture()
def write_caps(monkeypatch):
    """public_reply joins cap 3 per account; aggregate = max(3, round(0.6·3·members)).

    Patched at `safety_config.rate_limit_for_profile`, the same function the live
    worker reads through `budget.effective_cap`, so the config yaml cannot make the
    test drift. members=1 (the factory's account_count=0 → 1), so the aggregate cap
    is also 3 — enough to make it bind in test 2 by pre-filling it.
    """
    from app.services import budget

    monkeypatch.setattr(budget.safety_config, "rate_limit_for_profile",
                        lambda profile, use_case: {"joins_per_day": 3})
    monkeypatch.setattr(budget.safety_config, "get_premium_ceilings", lambda: {})


@pytest.fixture()
def read_caps(monkeypatch):
    """The read-budget gate reads `safety_config.get_read_limits()` — patch it there."""
    from app.core import safety_config

    monkeypatch.setattr(safety_config, "get_read_limits",
                        lambda: {"get_chat_info": 2})


# ── helpers ────────────────────────────────────────────────────────────────────

async def _mk_task(session_maker, account_id: int, task_type: str,
                   webhook: str | None = HOOK) -> int:
    async with session_maker() as s:
        async with s.begin():
            t = Task(
                external_id=uuid.uuid4().hex,
                account_id=account_id,
                task_type=task_type,
                payload={},
                status=TaskStatus.QUEUED,
                webhook_url=webhook,
                priority=5,
            )
            s.add(t)
            await s.flush()
            return t.id


async def _task_row(session_maker, task_id: int) -> Task:
    async with session_maker() as s:
        return (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()


async def _api_id(session_maker, cred_id: int) -> int:
    async with session_maker() as s:
        cred = await s.get(ApiCredential, cred_id)
        assert cred is not None
        return cred.api_id


async def _all_deliveries(session_maker) -> list[WebhookDelivery]:
    async with session_maker() as s:
        return (await s.execute(select(WebhookDelivery))).scalars().all()


def _by_event(deliveries, event: str) -> list[WebhookDelivery]:
    return [d for d in deliveries if d.payload.get("event") == event]


def _ctx(session_maker, fake_redis):
    return {"session_maker": session_maker, "redis": fake_redis}


def _no_action(payload):
    async def _action(client):  # pragma: no cover — вызов означал бы провал теста
        raise AssertionError("deferred по бюджету задача не должна доходить до Telegram")

    return _action


# ── §5 п.1: пер-аккаунтный бюджет исчерпан → task_deferred на webhook_url ──────

@pytest.mark.asyncio
async def test_per_account_defer_notifies_the_caller(
    write_caps, account_factory, session_maker
):
    from app.workers.base_task import run_task

    ids = await account_factory(use_case="public_reply")
    redis = FakeRedis()
    # Кап 3 исчерпан заранее; отказ не списывает (nothing is spent on refusal),
    # поэтому счётчик остаётся 3 на оба прогона.
    redis.store[f"rate:budget:acct:{ids['account_id']}:joins_per_day"] = 3
    task_id = await _mk_task(session_maker, ids["account_id"], "join_group")

    out = await run_task(_ctx(session_maker, redis), task_id, _no_action)

    assert out.get("rate_limited") is True
    row = await _task_row(session_maker, task_id)
    assert row.status == TaskStatus.DEFERRED
    assert row.error_code == "BUDGET_PER_ACCOUNT"

    deferred = [d for d in await _all_deliveries(session_maker)
                if d.payload.get("event") == "task_deferred"
                and d.payload.get("task_id") == row.external_id]
    assert len(deferred) == 1, "ровно одно уведомление о первом откладывании"
    assert deferred[0].url == HOOK, "заказчику, а не на system-URL"
    payload = deferred[0].payload
    assert set(payload) == {
        "event", "task_id", "account_id", "error_code", "deferred_until",
    }, "конверт из пяти полей, без расширений «на будущее»"
    assert payload["error_code"] == "BUDGET_PER_ACCOUNT"
    assert payload["account_id"] == ids["account_id"]
    assert datetime.fromisoformat(payload["deferred_until"]) == row.deferred_until, (
        "deferred_until — тот же until, что записан в задачу")


# ── §5 п.2: исчерпан агрегат → BUDGET_AGGREGATE ────────────────────────────────

@pytest.mark.asyncio
async def test_aggregate_defer_reports_the_aggregate_binding(
    write_caps, account_factory, session_maker
):
    from app.workers.base_task import run_task

    ids = await account_factory(use_case="public_reply")
    api_id = await _api_id(session_maker, ids["cred_id"])
    redis = FakeRedis()
    # Пер-аккаунтный чист, агрегат api_id исчерпан (кап при members=1 тоже 3):
    # скрипт отказывает на индексе 2 → binding=aggregate.
    redis.store[f"rate:budget:api:{api_id}:public_reply:joins_per_day"] = 3
    task_id = await _mk_task(session_maker, ids["account_id"], "join_group")

    out = await run_task(_ctx(session_maker, redis), task_id, _no_action)

    assert out.get("rate_limited") is True
    row = await _task_row(session_maker, task_id)
    assert row.status == TaskStatus.DEFERRED
    assert row.error_code == "BUDGET_AGGREGATE"

    deferred = [d for d in await _all_deliveries(session_maker)
                if d.payload.get("event") == "task_deferred"
                and d.payload.get("task_id") == row.external_id]
    assert len(deferred) == 1
    assert deferred[0].url == HOOK
    assert deferred[0].payload["error_code"] == "BUDGET_AGGREGATE", (
        "error_code в конверте — значение task.error_code (единый источник)")


# ── §5 п.3: read-ветка → READ_BUDGET_EXCEEDED ──────────────────────────────────

@pytest.mark.asyncio
async def test_read_budget_defer_notifies_the_caller(
    read_caps, account_factory, session_maker
):
    from app.workers.base_task import run_task

    ids = await account_factory(use_case="public_reply")
    redis = FakeRedis()
    redis.store[f"rate:read:get_chat_info:{ids['account_id']}"] = 2  # кап 2 исчерпан
    task_id = await _mk_task(session_maker, ids["account_id"], "get_chat_info")

    out = await run_task(_ctx(session_maker, redis), task_id, _no_action,
                         read_action="get_chat_info")

    assert out.get("rate_limited") is True
    row = await _task_row(session_maker, task_id)
    assert row.status == TaskStatus.DEFERRED
    assert row.error_code == "READ_BUDGET_EXCEEDED"

    deferred = [d for d in await _all_deliveries(session_maker)
                if d.payload.get("event") == "task_deferred"
                and d.payload.get("task_id") == row.external_id]
    assert len(deferred) == 1
    assert deferred[0].url == HOOK
    assert deferred[0].payload["error_code"] == "READ_BUDGET_EXCEEDED"


# ── §5 п.4: регресс — flood и complete своё не сменили ─────────────────────────

@pytest.mark.asyncio
async def test_flood_branch_still_sends_its_own_envelope_only(
    write_caps, account_factory, session_maker, monkeypatch
):
    """Flood-ветка шлёт ровно один свой конверт и ни одного task_deferred.

    Соседнюю отправку (flood_wait) не трогали — тест фиксирует её фактическую форму
    как регрессионный замок.
    """
    from app.services.stateless_manager import StatelessManager
    from app.workers import _tg_errors as tg
    from app.workers.base_task import run_task

    async def _flood(self, account_id, action, db):
        raise tg.FloodWait(value=42)

    monkeypatch.setattr(StatelessManager, "execute", _flood)

    ids = await account_factory(use_case="public_reply")
    task_id = await _mk_task(session_maker, ids["account_id"], "join_group")

    out = await run_task(_ctx(session_maker, FakeRedis()), task_id, _no_action)

    assert out.get("flood_until")
    row = await _task_row(session_maker, task_id)
    assert row.status == TaskStatus.DEFERRED
    assert row.error_code == "FLOOD_WAIT"

    deliveries = await _all_deliveries(session_maker)
    mine = [d for d in deliveries if d.payload.get("task_id") == row.external_id]
    floods = [d for d in mine if d.payload.get("event") == "flood_wait"]
    assert len(floods) == 1, "ровно один flood_wait"
    assert floods[0].url == HOOK
    # Фактическая форма отправки flood_wait (base_task, ветка FloodWait): четыре поля.
    assert set(floods[0].payload) == {"event", "task_id", "account_id", "flood_until"}
    assert not [d for d in mine if d.payload.get("event") == "task_deferred"], (
        "flood-ветка не шлёт task_deferred")


@pytest.mark.asyncio
async def test_complete_branch_sends_no_task_deferred(
    write_caps, account_factory, session_maker, monkeypatch
):
    """Успешное исполнение — прежний task_complete, без бюджетного уведомления."""
    from app.services.stateless_manager import StatelessManager
    from app.workers.base_task import run_task

    async def _ok(self, account_id, action, db):
        return {"done": True}

    monkeypatch.setattr(StatelessManager, "execute", _ok)

    ids = await account_factory(use_case="public_reply")
    task_id = await _mk_task(session_maker, ids["account_id"], "join_group")

    out = await run_task(_ctx(session_maker, FakeRedis()), task_id, _no_action)

    assert out.get("done") is True
    row = await _task_row(session_maker, task_id)
    assert row.status == TaskStatus.COMPLETE

    deliveries = await _all_deliveries(session_maker)
    mine = [d for d in deliveries if d.payload.get("task_id") == row.external_id]
    completes = [d for d in mine if d.payload.get("event") == "task_complete"]
    assert len(completes) == 1 and completes[0].url == HOOK
    assert not [d for d in mine if d.payload.get("event") == "task_deferred"], (
        "ветка task_complete вебхук task_deferred не шлёт")


# ── §5 п.5: повторный defer — вторая строка, другой deferred_until ─────────────

@pytest.mark.asyncio
async def test_second_defer_of_the_same_task_sends_a_second_event(
    write_caps, account_factory, session_maker
):
    """Перепланированная задача, снова упёршаяся в бюджет, шлёт новое событие.

    Семантика at-least-once: по одному task_deferred на каждый defer, приёмник
    дедуплицирует по паре (task_id, deferred_until). Перевод в QUEUED повторяет
    reenqueue_due_deferred (status=QUEUED, deferred_until=None).
    """
    from app.workers.base_task import run_task

    ids = await account_factory(use_case="public_reply")
    redis = FakeRedis()
    redis.store[f"rate:budget:acct:{ids['account_id']}:joins_per_day"] = 3
    task_id = await _mk_task(session_maker, ids["account_id"], "join_group")

    await run_task(_ctx(session_maker, redis), task_id, _no_action)
    first = await _task_row(session_maker, task_id)
    first_until = first.deferred_until

    async with session_maker() as s:  # «перепланировка» планировщиком
        async with s.begin():
            t = await s.get(Task, task_id)
            t.status = TaskStatus.QUEUED
            t.deferred_until = None

    await run_task(_ctx(session_maker, redis), task_id, _no_action)

    row = await _task_row(session_maker, task_id)
    assert row.status == TaskStatus.DEFERRED

    deferred = [d for d in await _all_deliveries(session_maker)
                if d.payload.get("event") == "task_deferred"
                and d.payload.get("task_id") == row.external_id]
    assert len(deferred) == 2, "по одному событию на каждый defer"
    seconds = sorted(datetime.fromisoformat(d.payload["deferred_until"]) for d in deferred)
    assert seconds[0] == first_until
    assert seconds[1] > seconds[0], "второй defer уезжает с новым сроком"
