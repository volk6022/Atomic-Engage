"""Honest fixtures for the 002-fix-extend TDD suite.

Unlike the legacy tests/conftest.py, these fixtures NEVER swallow infrastructure
errors and NEVER yield None on failure. If Postgres/Redis is unavailable a test
is explicitly skipped (with a reason) — it can never silently "pass" (FR-130).
"""
import os
import pathlib

import pytest

import app


APP_ROOT = pathlib.Path(list(app.__path__)[0]).resolve()         # .../fleet_manager/app
PROJECT_ROOT = APP_ROOT.parent                                   # .../fleet_manager


def read_source(relative_to_app: str) -> str:
    """Return the source text of a module file under app/ (for defect pinning)."""
    return (APP_ROOT / relative_to_app).read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://fleet_user:fleet_password@localhost:5434/fleet_test",
    )


@pytest.fixture
async def pg_conn(database_url):
    """A live asyncpg-backed SQLAlchemy connection, or an explicit skip (never None)."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            yield conn
    except Exception as exc:  # noqa: BLE001 — translate to an honest skip, do not pass
        pytest.skip(f"PostgreSQL unavailable for integration test: {exc}")
    finally:
        await engine.dispose()


from types import SimpleNamespace  # noqa: E402


class _FakeClient:
    """Fake kurigram client (the only sanctioned non-persistence mock — T202/FR-135).

    Behaviour is driven by a shared control dict: set ctl['raise'] to an exception to
    simulate FloodWait/ban/PeerIdInvalid; set ctl['msg_id'] for send results.
    """

    def __init__(self, ctl):
        self.ctl = ctl

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _maybe_raise(self):
        exc = self.ctl.get("raise")
        if exc is not None:
            raise exc

    async def send_message(self, target, text, reply_to_message_id=None):
        await self._maybe_raise()
        return SimpleNamespace(id=self.ctl.get("msg_id", 123))

    async def join_chat(self, target):
        await self._maybe_raise()
        return SimpleNamespace(id=555)

    async def send_reaction(self, *a, **k):
        await self._maybe_raise()
        return True

    async def add_chat_members(self, *a, **k):
        await self._maybe_raise()
        return SimpleNamespace(id=777)

    async def get_users(self, username):
        await self._maybe_raise()
        # ctl['users'] lets a test simulate a non-user (channel) -> empty result.
        if "users" in self.ctl:
            return self.ctl["users"]
        return [SimpleNamespace(id=42, access_hash=99, is_bot=False,
                                first_name="Alice", last_name=None,
                                is_verified=False, is_scam=False)]

    async def get_chat(self, chat_id):
        await self._maybe_raise()
        if "chat" in self.ctl:
            return self.ctl["chat"]
        from pyrogram import enums
        return SimpleNamespace(
            id=self.ctl.get("chat_id", 93372553),
            type=enums.ChatType.CHANNEL,
            title="ACME",
            username="acme",
            description="About ACME. Reach us at hi@acme.ru or https://acme.ru tel +79991234567",
            bio=None,
            members_count=self.ctl.get("members_count", 5821),
            is_verified=False,
            is_scam=False,
            linked_chat=SimpleNamespace(username="acme_chat"),
            pinned_message=SimpleNamespace(text="Pinned: visit acme.ru/jobs", caption=None),
        )

    async def get_chat_members_count(self, chat_id):
        await self._maybe_raise()
        return self.ctl.get("members_count", 5821)

    async def get_chat_history(self, chat_id, limit=0, offset_id=0, min_id=0):
        await self._maybe_raise()
        from datetime import datetime, timezone
        from pyrogram import enums
        posts = self.ctl.get("history")
        if posts is None:
            base = datetime(2026, 6, 14, tzinfo=timezone.utc)
            posts = [
                SimpleNamespace(id=412, date=base, text="Hiring CV engineer, mail jobs@acme.ru",
                                caption=None, views=1503, forwards=2,
                                media=enums.MessageMediaType.PHOTO),
                SimpleNamespace(id=411, date=datetime(2026, 4, 2, tzinfo=timezone.utc),
                                text="Old post https://acme.ru/news", caption=None,
                                views=900, forwards=0, media=None),
            ]
        for m in posts:
            if min_id and m.id <= min_id:
                continue
            yield m

    async def get_me(self):
        await self._maybe_raise()
        return SimpleNamespace(id=1)

    async def resolve_peer(self, peer_id):
        return SimpleNamespace(user_id=peer_id)


@pytest.fixture
def fake_tg():
    """Patch StatelessManager's client factory with a fake; yield the control dict."""
    from app.services import stateless_manager

    ctl = {"raise": None, "msg_id": 123}

    def factory(account, credential, proxy):
        return _FakeClient(ctl)

    stateless_manager.set_client_factory(factory)
    yield ctl
    stateless_manager.reset_client_factory()


class _FakeArqRedis:
    """Minimal stand-in for the ARQ redis pool used by enqueue/peer-cache calls."""

    def __init__(self):
        self.jobs = []
        self.kv = {}

    async def enqueue_job(self, func, **kwargs):
        self.jobs.append((func, kwargs))

    async def incr(self, key):
        self.kv[key] = int(self.kv.get(key, 0)) + 1
        return self.kv[key]

    async def expire(self, key, ttl):
        return True

    async def eval(self, script, numkeys, *args):
        # Emulate the atomic rate-limit Lua (_RATE_LIMIT_LUA): INCR the key and
        # set EXPIRE on first write. The fake has no real TTL, so we only mirror
        # the returned counter the production call relies on (FR-350).
        key = args[0]
        self.kv[key] = int(self.kv.get(key, 0)) + 1
        return self.kv[key]

    async def set(self, key, value, *a, **k):
        self.kv[key] = value

    async def setex(self, key, ttl, value):
        self.kv[key] = value

    async def get(self, key):
        return self.kv.get(key)


@pytest.fixture
def fake_redis():
    return _FakeArqRedis()


@pytest.fixture
async def redis_conn():
    import redis.asyncio as redis

    url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    client = redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis unavailable for integration test: {exc}")
    yield client
    await client.flushdb()
    await client.aclose()
