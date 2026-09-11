"""Honest test fixtures (FR-130/131/134).

Differences from the original conftest, which silently passed integration tests with
no database:

* Deterministic env is set BEFORE any app import (FR-134) — no dependency on a
  developer `.env`.
* Infrastructure fixtures never hand back a null session and never swallow errors: if
  Postgres is unavailable the dependent tests are explicitly skipped via pytest.skip
  (FR-130). Redis skips are stricter — an explicitly provided REDIS_URL that does not
  answer is a hard error; the conftest default may skip, but the skip count is always
  printed in the session summary (see _refuse_or_skip / pytest_terminal_summary).
* The schema comes from real Alembic migrations (the partitioned peer_access_hashes),
  applied with `alembic upgrade head` — not a blanket metadata build (FR-131).
"""
import os
import pathlib
import sys

# --- deterministic env, set before importing anything from `app` (FR-134) ----------
os.environ.setdefault("API_KEY", "change_me_in_production")
os.environ.setdefault(
    "N8N_SYSTEM_WEBHOOK_URL", "https://your-n8n-instance.com/webhook/fleet"
)
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://fleet_user:fleet_password@localhost:5434/fleet_test",
)
# Captured BEFORE the setdefault below: an explicitly exported REDIS_URL is the
# developer's statement "Redis is here", and if it doesn't answer that is an
# environment error to fail on, not a reason to skip (see _refuse_or_skip and
# fleet_manager/.env.test.example).
REDIS_URL_EXPLICIT = "REDIS_URL" in os.environ
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
# Human pacing is real-time at TIME_SCALE=1; off by default in tests so the
# inter-action floor (60-300 s) doesn't sleep real minutes. The dedicated humanizer
# wiring test flips the flag on with a spy (no real sleep).
os.environ.setdefault("HUMANIZE_ACTIONS", "false")

import asyncio  # noqa: E402
import subprocess  # noqa: E402
from collections.abc import AsyncGenerator  # noqa: E402
from datetime import datetime  # noqa: E402
import random  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
import respx  # noqa: E402
from httpx import AsyncClient, ASGITransport  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

from app.db.models import Account, ApiCredential, Proxy  # noqa: E402


FLEET_ROOT = pathlib.Path(__file__).resolve().parent.parent  # .../fleet_manager
TEST_DATABASE_URL = os.environ["DATABASE_URL"]
TEST_REDIS_URL = os.environ["REDIS_URL"]

# --- Redis reachability: one honest probe per session -------------------------------
#
# On some dev machines Windows reserves the 6300-6399 TCP range (check with
# `netsh int ipv4 show excludedportrange protocol=tcp`), so nothing — including
# Docker — can bind 6379 there. On such a machine the four redis tests used to
# skip silently on every run. The contract now:
#
# * REDIS_URL set explicitly + unreachable  -> hard error (never a silent green);
# * REDIS_URL left to the conftest default + unreachable -> redis-dependent tests
#   skip, and pytest_terminal_summary prints ONE line at session end with the
#   count and the URL, so the skips are always visible in the summary.
#
# The probe is a single cached PING with a short socket timeout. It deliberately
# does not go through arq: arq's create_pool retries a dead endpoint conn_retries=5
# times with conn_retry_delay sleeps before raising (~10-25 s per call), which is
# exactly the stall this file exists to prevent.
_REDIS_PROBE: tuple[bool, str] | None = None
_redis_skipped = 0


def _probe_redis() -> tuple[bool, str]:
    """(reachable, error) for TEST_REDIS_URL, decided once per session."""
    global _REDIS_PROBE
    if _REDIS_PROBE is None:
        import redis as redis_sync

        try:
            client = redis_sync.Redis.from_url(
                TEST_REDIS_URL,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            try:
                client.ping()
            finally:
                client.close()
            _REDIS_PROBE = (True, "")
        except Exception as exc:  # noqa: BLE001 — any failure means "unavailable"
            _REDIS_PROBE = (False, str(exc))
    return _REDIS_PROBE


def _refuse_or_skip(reason: str) -> None:
    """Skip policy for redis-dependent tests (FR-130, tightened).

    An explicitly provided REDIS_URL that does not answer fails the session loudly.
    The conftest default address may skip honestly, but the skip is counted and
    reported by pytest_terminal_summary so a green run can never hide it.
    """
    global _redis_skipped
    if REDIS_URL_EXPLICIT:
        pytest.fail(
            f"REDIS_URL задан явно ({TEST_REDIS_URL}), но Redis недоступен: {reason}. "
            "Адрес, заданный вручную, — это утверждение «Redis здесь», поэтому это "
            "ошибка окружения, а не повод для пропуска (см. .env.test.example)."
        )
    _redis_skipped += 1
    pytest.skip(f"Redis unavailable at {TEST_REDIS_URL}: {reason}")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Session-final one-liner for redis skips, e.g.

    4 redis-тестов пропущено: Redis недоступен по redis://localhost:6379

    Note: on consoles whose encoding has no Cyrillic (e.g. cp1252), pytest
    degrades the text to ASCII \\u-escapes (its own write_raw fallback) — the
    count and URL stay readable there too.
    """
    if _redis_skipped:
        # Newline separately from the message: TerminalWriter escapes the whole
        # line on encode failure, and a "\n" inside it would come out as "\n" text.
        terminalreporter.ensure_newline()
        terminalreporter.write_line(
            f"{_redis_skipped} redis-тестов пропущено: "
            f"Redis недоступен по {TEST_REDIS_URL}"
        )


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _run_alembic_upgrade() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(FLEET_ROOT),
        env={**os.environ},
        capture_output=True,
        text=True,
    )


@pytest_asyncio.fixture(scope="session")
async def db_engine():
    """Migrated test database. Skips honestly (never None) when Postgres is absent."""
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — honest skip, never a silent pass
        await engine.dispose()
        pytest.skip(f"PostgreSQL unavailable at {TEST_DATABASE_URL}: {exc}")

    # clean slate, then real migrations (partitioned peer_access_hashes)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))

    result = _run_alembic_upgrade()
    if result.returncode != 0:
        await engine.dispose()
        pytest.fail(
            "alembic upgrade head failed (FR-131):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

    yield engine

    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def session_maker(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db_session(session_maker) -> AsyncGenerator[AsyncSession, None]:
    async with session_maker() as session:
        async with session.begin():
            yield session
            await session.rollback()


@pytest_asyncio.fixture
async def redis_client():
    import redis.asyncio as redis

    # Decide from the cached session probe first: without it every redis test pays
    # the full dead-port connect (~4 s each) just to learn the same answer.
    reachable, probe_err = _probe_redis()
    if not reachable:
        _refuse_or_skip(probe_err)

    client = redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001 — honest skip/fail, see _refuse_or_skip
        await client.aclose()
        _refuse_or_skip(f"{exc}")
    yield client
    await client.flushdb()
    await client.aclose()


class _DeadRedisPoolStub:
    """Stand-in for the cached arq pool in app.services.webhook_queue.

    The producer contract treats a failed enqueue as best-effort: the
    WebhookDelivery row is the durable record and stays `pending` for the redrive
    cron (webhook_queue.py's except-branch). With Redis down, letting the real
    `_get_pool` run means arq's create_pool retry loop (~10-25 s) inside every
    test whose run_task/producer path reaches enqueue_webhook. The stub keeps that
    contract observable (row persisted, enqueue attempted-and-"sent") and the
    suite fast.
    """

    async def enqueue_job(self, *args, **kwargs):
        return None


@pytest.fixture(autouse=True)
def _no_arq_retry_stall_without_redis(monkeypatch):
    """Tests that go through the queue must be honest AND fast when Redis is down.

    Under pytest no arq worker consumes the queue, so a dead Redis buys producer
    tests nothing except arq's connection retries: measured on this machine, the
    six test_deferred_webhook.py tests alone burned ~82 s per run. When the probe
    says Redis answers, this fixture does nothing at all.
    """
    reachable, _ = _probe_redis()
    if reachable:
        yield
        return

    from app.services import webhook_queue

    monkeypatch.setattr(webhook_queue, "_pool", _DeadRedisPoolStub())
    yield


@pytest_asyncio.fixture
async def async_client(session_maker):
    from app.main import app
    from app.api.deps import get_db_dep

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db_dep] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def respx_mock():
    with respx.mock(assert_all_mocked=False):
        yield respx


@pytest.fixture
def account_factory(session_maker):
    """Async factory creating a persisted Account (+ ApiCredential + Proxy).

    Usage: ids = await account_factory(phone_country="RU", proxy_country="US")
    Returns: {"account_id": int, "proxy_id": int, "cred_id": int}
    """

    async def _make(
        *,
        phone_country: str = "RU",
        proxy_country: str = "RU",
        status: str = "active",
        warmup_tier: str = "ready",
        use_case: str = "reactions",
        flood_until: datetime | None = None,
        proxy_is_healthy: bool = True,
        proxy_state: str = "assigned",
        warmup_day: int = 0,
    ) -> dict:
        async with session_maker() as session:
            async with session.begin():
                cred = ApiCredential(
                    api_id=random.randint(10_000_000, 99_999_999),
                    api_hash="a" * 64,
                    account_count=0,
                )
                session.add(cred)
                await session.flush()

                proxy = Proxy(
                    url=(
                        f"socks5://user:pass@"
                        f"10.{random.randint(0,255)}.{random.randint(0,255)}"
                        f".{random.randint(1,254)}:1080"
                    ),
                    proxy_type="residential",
                    country=proxy_country,
                    tz_offset=10800 if proxy_country == "RU" else 0,
                    state=proxy_state,
                    is_healthy=proxy_is_healthy,
                )
                session.add(proxy)
                await session.flush()

                account = Account(
                    status=status,
                    warmup_tier=warmup_tier,
                    use_case=use_case,
                    phone=f"+7{random.randint(9_000_000_000, 9_999_999_999)}",
                    phone_country=phone_country,
                    session_string="test_session_string",
                    api_credential_id=cred.id,
                    proxy_id=proxy.id,
                    device_model="Samsung Galaxy S21",
                    system_version="12",
                    app_version="9.1.0",
                    lang_code="ru",
                    system_lang_code="ru-RU",
                    flood_until=flood_until,
                    warmup_day=warmup_day,
                    work_start=0,
                    work_end=24,  # always in-window: keeps worker tests time-independent
                )
                session.add(account)
                await session.flush()

                return {
                    "account_id": account.id,
                    "proxy_id": proxy.id,
                    "cred_id": cred.id,
                }

    return _make
