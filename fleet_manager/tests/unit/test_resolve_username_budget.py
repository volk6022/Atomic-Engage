"""E5: `resolve_username` must spend its declared read budget.

The worker used to call `run_task` without `read_action`, so username resolves
were invisible to the per-account daily read budget and ran uncapped forever.
Passing `read_action="resolve_username"` wires the worker into the existing gate
(`base_task._read_budget_exceeded`, key `rate:read:resolve_username:{account_id}`,
cap `read_limits.resolve_username`): within budget the task runs; over budget it
goes DEFERRED for +1 h with `error_code=READ_BUDGET_EXCEEDED` (base_task read
branch, §4.1).

Two deliberate side effects of the same one-line change are pinned here as well
(E5 §2 of the contract — "оба обязаны быть отражены в тесте"):

* `account_facing` becomes False, so the datacenter-ASN block-list (FR-310) stops
  cutting this warmup-exempt read: a resolve over a datacenter exit must COMPLETE,
  not put the account to SLEEPING with an asn_block webhook;
* the read counter increments BEFORE the cap comparison, so a refused attempt also
  consumes: after the deferred run the key reads cap + 1, never stuck at cap.

Harness (self-contained unit harness — no live Postgres, no Redis, no arq):

An earlier draft of this file borrowed the real-Postgres fixtures
(`account_factory`/`session_maker` from tests/conftest.py). That broke the unit
suite two ways: (a) it made the tests need raised infrastructure, and (b) the
conftest's session-scoped async fixtures do not survive the installed
pytest-asyncio 1.3.0 loop-scope semantics — the shared schema was dropped and
rebuilt mid-suite/mid-test, surfacing as random `relation "tasks"/"api_credentials"
does not exist` failures in every DB-backed unit file. The resume for E5 asked to
solve this within the unit suite, so the DB layer here is a per-test in-memory
sqlite database built from `Base.metadata.create_all`.

Two bounded compromises, both documented:

* JSONB DDL is postgres-only; on the sqlite dialect it is compiled as JSON via a
  `@compiles` shim scoped to this module's import. Values still bind/read through
  the generic JSON machinery, so the contract's payload/result round-trips hold.
* `BaseTask._claim_for_execution` opens with `SELECT pg_advisory_xact_lock(:k)` —
  a Postgres primitive with no sqlite equivalent. It is replaced by the same
  claim minus the lock primitive (EXECUTING write + commit). The FIFO/advisory-lock
  behaviour is orthogonal to E5's budget contract and is exercised by the
  integration suite against real Postgres; what this file must prove is budget
  consumption, deferral and the deferred webhook, and those paths run unpatched.

FakeRedis follows tests/unit/test_budget_consume_is_atomic.py: it honours exactly
the one Lua script the read path issues and raises on anything else — a fake that
answers scripts it does not know describes a world that does not exist. The arq
pool behind `enqueue_webhook` is stubbed for the same reason: the delivery ROW is
the contract here, and a real pool would stall the harness on connection retries
against a broker the unit run does not raise.
"""
import random
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.core import safety_config
from app.core.constants import AccountStatus, TaskStatus
from app.db.models import Account, ApiCredential, Base, Proxy, Task, WebhookDelivery
from sqlalchemy import BigInteger


# ── sqlite DDL shims ──────────────────────────────────────────────────────────────


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(element, compiler, **kw):
    """Render JSONB columns as JSON on sqlite (generic JSON bind/result machinery)."""
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_on_sqlite(element, compiler, **kw):
    """INTEGER PRIMARY KEY is sqlite's only autoincremented (rowid-aliased) PK form."""
    return "INTEGER"


# ── fakes ─────────────────────────────────────────────────────────────────────────


class _FakeRedis:
    """Minimal arq-redis: the INCR+first-write-EXPIRE Lua script, job enqueue, SETEX.

    Anything else raises — a fake that answers scripts it does not know describes a
    world that does not exist (same policy as tests/unit/test_budget_consume_is_atomic.py).
    """

    def __init__(self) -> None:
        self.kv: dict[str, object] = {}
        self.ttl: dict[str, int] = {}
        self.jobs: list[tuple[str, dict]] = []

    async def eval(self, script, numkeys, *args):
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])
        if "INCR" in script and len(keys) == 1 and len(argv) == 1:
            key = keys[0]
            self.kv[key] = int(self.kv.get(key, 0)) + 1
            if self.kv[key] == 1:
                self.ttl[key] = int(argv[0])
            return self.kv[key]
        raise AssertionError(
            f"test resolved a redis command the read path must not issue: "
            f"{script.strip()[:60]!r}"
        )

    async def enqueue_job(self, func, **kwargs):
        self.jobs.append((func, kwargs))

    async def setex(self, key, ttl, value):
        self.kv[key] = value

    async def set(self, key, value, *a, **k):
        self.kv[key] = value

    async def get(self, key):
        return self.kv.get(key)


class _FakeClient:
    """Fake kurigram client: get_users finds Alice, get_chat fakes a channel."""

    def __init__(self, ctl):
        self.ctl = ctl

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_users(self, username):
        if self.ctl.get("raise"):
            raise self.ctl["raise"]
        if "users" in self.ctl:
            return self.ctl["users"]
        return [SimpleNamespace(id=42, access_hash=99, is_bot=False,
                                first_name="Alice", last_name=None,
                                is_verified=False, is_scam=False)]

    async def get_chat(self, username):
        if self.ctl.get("raise"):
            raise self.ctl["raise"]
        from pyrogram import enums
        return SimpleNamespace(
            id=93372553, type=enums.ChatType.CHANNEL, title="ACME",
            username="acme", access_hash=99, is_verified=False, is_scam=False,
        )


# ── fixtures ──────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session_maker():
    """Per-test in-memory sqlite schema — no shared state, no live infrastructure.

    Shadows tests/conftest.py's Postgres-backed fixture of the same name: the
    conftest session fixture's lifecycle is what destabilised the suite (see
    module docstring), and a unit harness must not require a raised database.
    """
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def sqlite_claim(monkeypatch):
    """The claim path without its pg-only advisory-lock primitive (see docstring)."""
    from app.workers import base_task

    async def _claim_without_pg_lock(db, task):
        task.status = TaskStatus.EXECUTING
        task.started_at = base_task._now()
        await db.commit()
        return True

    monkeypatch.setattr(
        base_task.BaseTask, "_claim_for_execution",
        staticmethod(_claim_without_pg_lock),
    )


@pytest.fixture(autouse=True)
def arq_pool_stub(monkeypatch):
    """`enqueue_webhook` persists the delivery row, then hands it to the arq pool.

    The row is the contract; the pool is infrastructure. A stub keeps the harness
    off live-connection retries while `enqueue_next`'s job record stays observable.
    """
    from app.services import webhook_queue

    class _StubPool:
        def __init__(self) -> None:
            self.jobs: list[tuple[str, dict]] = []

        async def enqueue_job(self, name, *args, **kwargs):
            self.jobs.append((name, kwargs))

    stub = _StubPool()
    monkeypatch.setattr(webhook_queue, "_pool", stub)
    return stub


@pytest.fixture
def fake_redis():
    return _FakeRedis()


@pytest.fixture
def fake_tg():
    from app.services import stateless_manager

    ctl: dict = {}
    stateless_manager.set_client_factory(lambda account, credential, proxy: _FakeClient(ctl))
    yield ctl
    stateless_manager.reset_client_factory()


@pytest.fixture
def cap_one(monkeypatch):
    """read_limits.resolve_username = 1 — the smallest cap that can be pierced."""
    monkeypatch.setattr(safety_config, "get_read_limits", lambda: {"resolve_username": 1})


# ── helpers ───────────────────────────────────────────────────────────────────────


async def _mk_account(session_maker, *, proxy_asn: int | None = None) -> int:
    """Account + ApiCredential + Proxy, mirroring tests/conftest.py's factory."""
    async with session_maker() as s:
        async with s.begin():
            cred = ApiCredential(
                api_id=random.randint(10_000_000, 99_999_999),
                api_hash="a" * 64,
                account_count=0,
            )
            s.add(cred)
            await s.flush()

            proxy = Proxy(
                url=(
                    f"socks5://user:pass@"
                    f"10.{random.randint(0, 255)}.{random.randint(0, 255)}"
                    f".{random.randint(1, 254)}:1080"
                ),
                proxy_type="residential",
                country="RU",
                tz_offset=10800,
                state="assigned",
                is_healthy=True,
            )
            if proxy_asn is not None:
                proxy.asn = proxy_asn
            s.add(proxy)
            await s.flush()

            account = Account(
                status="active",
                warmup_tier="ready",
                use_case="reactions",
                phone=f"+7{random.randint(9_000_000_000, 9_999_999_999)}",
                phone_country="RU",
                session_string="test_session_string",
                api_credential_id=cred.id,
                proxy_id=proxy.id,
                device_model="Samsung Galaxy S21",
                system_version="12",
                app_version="9.1.0",
                lang_code="ru",
                system_lang_code="ru-RU",
                flood_until=None,
                warmup_day=0,
                work_start=0,
                work_end=24,  # always in-window: keeps worker tests time-independent
            )
            s.add(account)
            await s.flush()
            return account.id


async def _mk_task(session_maker, account_id: int) -> int:
    async with session_maker() as s:
        async with s.begin():
            t = Task(
                external_id=uuid.uuid4().hex,
                account_id=account_id,
                task_type="resolve_username",
                payload={"username": "alice"},
                status=TaskStatus.QUEUED,
                webhook_url="https://hook.test/result",
                priority=5,
            )
            s.add(t)
            await s.flush()
            return t.id


def _ctx(session_maker, fake_redis) -> dict:
    return {"session_maker": session_maker, "redis": fake_redis}


# ── harness 1: first run completes and spends exactly one unit ────────────────────


async def test_resolve_within_budget_completes_and_consumes_one(
    session_maker, fake_tg, fake_redis, cap_one
):
    from app.workers.resolve_username import resolve_username

    acc = await _mk_account(session_maker)
    tid = await _mk_task(session_maker, acc)

    result = await resolve_username(_ctx(session_maker, fake_redis), tid)

    assert result.get("peer_id") == 42                     # ran, not deferred
    async with session_maker() as s:
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert task.status == TaskStatus.COMPLETE
    # The literal key form is pinned on purpose: it is the contract's counter name
    # (base_task builds `read:{action}:{account_id}`, redis_client adds `rate:`).
    assert fake_redis.kv[f"rate:read:resolve_username:{acc}"] == 1


# ── harness 2: over budget → deferred, and the refused attempt still counts ───────


async def test_resolve_over_budget_defers_with_read_budget_exceeded(
    session_maker, fake_tg, fake_redis, cap_one
):
    from app.workers.resolve_username import resolve_username

    acc = await _mk_account(session_maker)

    tid1 = await _mk_task(session_maker, acc)
    r1 = await resolve_username(_ctx(session_maker, fake_redis), tid1)
    tid2 = await _mk_task(session_maker, acc)
    r2 = await resolve_username(_ctx(session_maker, fake_redis), tid2)

    assert r1.get("peer_id") == 42                         # first within budget
    assert r2.get("rate_limited") is True                  # second over the cap
    async with session_maker() as s:
        t1 = (await s.execute(select(Task).where(Task.id == tid1))).scalar_one()
        t2 = (await s.execute(select(Task).where(Task.id == tid2))).scalar_one()
    assert t1.status == TaskStatus.COMPLETE
    assert t2.status == TaskStatus.DEFERRED
    assert t2.error_code == "READ_BUDGET_EXCEEDED"
    assert t2.deferred_until is not None
    # Increment happens before the comparison (base_task._read_budget_exceeded):
    # the refused resolve also consumed, so the key reads cap + 1, not cap.
    assert fake_redis.kv[f"rate:read:resolve_username:{acc}"] == 2


# ── harness 3: the defer leaves a task_deferred webhook (E4, in-branch) ────────────


async def test_deferred_resolve_emits_task_deferred_webhook(
    session_maker, fake_tg, fake_redis, cap_one
):
    """E5 §5 п.3 together with E4: the over-budget run queues a `task_deferred`
    envelope addressed to the requester's webhook_url. E4's webhook calls are part
    of this branch (base_task read-budget branch), so the assertion is live."""
    from app.workers.resolve_username import resolve_username

    acc = await _mk_account(session_maker)

    tid1 = await _mk_task(session_maker, acc)
    await resolve_username(_ctx(session_maker, fake_redis), tid1)
    tid2 = await _mk_task(session_maker, acc)
    await resolve_username(_ctx(session_maker, fake_redis), tid2)

    async with session_maker() as s:
        t2 = (await s.execute(select(Task).where(Task.id == tid2))).scalar_one()
        deliveries = (
            await s.execute(select(WebhookDelivery).where(
                WebhookDelivery.url == "https://hook.test/result"))
        ).scalars().all()

    deferred = [d for d in deliveries if d.payload.get("event") == "task_deferred"]
    assert deferred, (
        f"task_deferred не доставлен; пришли только: "
        f"{[d.payload.get('event') for d in deliveries]}"
    )
    payload = deferred[0].payload
    assert payload["task_id"] == t2.external_id
    assert payload["account_id"] == acc
    assert payload["error_code"] == "READ_BUDGET_EXCEEDED"
    # Same `until` as written to the task — compared as instants: sqlite's
    # DateTime(timezone=True) round-trips naive (no offset), Postgres returns
    # UTC-aware; on both backends the envelope must carry that same instant.
    from datetime import datetime, timezone

    assert datetime.fromisoformat(payload["deferred_until"]) == (
        t2.deferred_until.replace(tzinfo=timezone.utc)
    )


# ── side effect §2.1: datacenter-ASN gate no longer cuts the resolve ───────────────


async def test_resolve_on_datacenter_asn_completes_without_asn_block(
    session_maker, fake_tg, fake_redis, cap_one
):
    """With read_action set, `account_facing` is False (base_task.run_task), so the
    FR-310 datacenter-ASN gate must not fire: before the change this task was
    deferred with ACCOUNT_BLOCKED and the account was put to SLEEPING."""
    from app.workers.resolve_username import resolve_username

    acc = await _mk_account(session_maker, proxy_asn=16509)  # AWS — block-listed

    tid = await _mk_task(session_maker, acc)
    result = await resolve_username(_ctx(session_maker, fake_redis), tid)

    assert result.get("peer_id") == 42             # ran over a datacenter exit
    async with session_maker() as s:
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
        account = (
            await s.execute(select(Account).where(Account.id == acc))
        ).scalar_one()
        blocks = (
            await s.execute(select(WebhookDelivery))
        ).scalars().all()
    assert task.status == TaskStatus.COMPLETE
    assert task.error_code != "ACCOUNT_BLOCKED"
    assert account.status == AccountStatus.ACTIVE  # not put to SLEEPING
    assert not [b for b in blocks if b.payload.get("event") == "asn_block"]
