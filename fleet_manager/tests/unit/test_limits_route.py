"""Harness for ``GET /v1/limits`` (contract E1 §5).

FakeRedis with get/ttl/pipeline after the atomicity suite's pattern
(``test_budget_consume_is_atomic.FakeRedis``); one account ``use_case="public_reply"``
on an api_credential with ``account_count=5``, so the aggregate join cap is
``round(0.6 · 3 · 5) = 9``. Every counter state in these tests is computed by hand and
asserted against the exact JSON — the dashboard is a витрина of the worker's own
numbers, so the harness pins shapes (key forms, response contract) rather than
re-deriving anything.

The key f-strings below are transcribed from the worker's own builders —
``budget.py:108-116`` (per-account + api_id aggregate, E6 shape) and
``base_task.py:231`` (reads), with the ``rate:`` prefix from ``redis_client.py:94`` —
which is what makes test 5 a byte-for-byte guard against name drift.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError

from app.api import deps
from app.api.v1 import limits
from app.core import safety_config
from app.core.config import get_settings
from app.core.safety_defaults import READ_LIMITS
from app.services import budget as budget_service

ACCOUNT_ID = 12
CRED_ID = 77
API_ID = 2040
ACCOUNT_COUNT = 5

# public_reply's write table — joins_per_day: 3 is the live number the aggregate
# formula below is computed from. Matches config/safety.yaml:84.
PUBLIC_REPLY_WRITE = {
    "reactions_per_day": 30,
    "messages_per_day": 10,
    "joins_per_day": 3,
    "invites_per_day": 0,
}
READ_CAPS = {
    "resolve_username": 100,
    "get_chat_info": 200,
    "get_chat_history": 2000,
    "get_similar_channels": 50,
    "search_public_chats": 50,
    "get_chat_admins": 100,
    "get_dialogs": 50,
}

SENSITIVE = sorted(budget_service.SENSITIVE_ACTIONS)  # messages/joins/invites per_day


# --- key builders: the SAME f-strings the worker's write path uses -------------------
def w_acct_key(account_id: int, action: str) -> str:
    # budget.py:108-109, `rate:` prefix added by redis_client (budget_consume, :94)
    return f"rate:budget:acct:{account_id}:{action}"


def w_api_key(api_id: int, use_case: str, action: str) -> str:
    # budget.py:114 — the E6 shape `budget:api:{api_id}:{use_case}:{action}`
    return f"rate:budget:api:{api_id}:{use_case}:{action}"


def read_key(action: str, account_id: int) -> str:
    # base_task.py:231
    return f"rate:read:{action}:{account_id}"


def expected_action_order() -> list[str]:
    # Imported from the sources, not retyped: 4 writes in WRITE_BUDGET_ACTION order,
    # then the 7 reads in READ_LIMITS key order (contract E1 §3).
    from app.workers.base_task import WRITE_BUDGET_ACTION

    return [*WRITE_BUDGET_ACTION.values(), *READ_LIMITS.keys()]


# --- fakes ---------------------------------------------------------------------------
class FakePipeline:
    """Queued GET/TTL commands, executed against the fake store in order."""

    def __init__(self, client: "FakeRedis") -> None:
        self._client = client
        self._commands: list[tuple[str, str]] = []

    def get(self, key: str) -> "FakePipeline":
        self._commands.append(("get", key))
        return self

    def ttl(self, key: str) -> "FakePipeline":
        self._commands.append(("ttl", key))
        return self

    async def execute(self) -> list[object]:
        if self._client.fail:
            raise RedisError("connection refused")
        out: list[object] = []
        for cmd, key in self._commands:
            self._client.requested.append(key)
            if cmd == "get":
                out.append(self._client.store.get(key))
            else:
                out.append(self._client.ttl.get(key, -2))  # −2: key does not exist
        return out


class FakeRedis:
    """Minimal redis.asyncio twin: exactly what rate_limit_peek_many may issue.

    Same discipline as the atomicity suite's FakeRedis: anything else raises, so a
    rewrite that starts mutating counters from the read path fails here instead of on
    prod. Records every full key it was asked about in ``requested`` — the key-shape
    test compares that against the worker's own f-strings.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.requested: list[str] = []
        self.fail = False

    def seed(self, key: str, value: int, ttl: int | None = None) -> None:
        self.store[key] = str(value)  # decode_responses=True: GET returns strings
        if ttl is not None:
            self.ttl[key] = ttl

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)


class StubAccount:
    def __init__(self, id: int, use_case: str = "public_reply",
                 api_credential_id: int = CRED_ID) -> None:
        self.id = id
        self.use_case = use_case
        self.api_credential_id = api_credential_id


class StubCred:
    def __init__(self, id: int = CRED_ID, api_id: int = API_ID,
                 account_count: int = ACCOUNT_COUNT) -> None:
        self.id = id
        self.api_id = api_id
        self.account_count = account_count


class FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class FakeSession:
    """Serves the route's single select(Account, ApiCredential) query."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def execute(self, _stmt) -> FakeResult:
        return FakeResult(self._rows)


def make_client(rows: list, fake_redis: FakeRedis) -> AsyncClient:
    app = FastAPI()
    app.include_router(limits.router)

    async def override_db():
        yield FakeSession(rows)

    async def override_redis():
        return fake_redis

    app.dependency_overrides[deps.get_db_dep] = override_db
    app.dependency_overrides[deps.get_redis_dep] = override_redis
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": get_settings().API_KEY},
    )


@pytest.fixture()
def caps(monkeypatch):
    """Cap sources patched at the module boundary, after the atomicity suite (:110)."""
    monkeypatch.setattr(safety_config, "rate_limit_for_profile",
                        lambda profile, use_case: dict(PUBLIC_REPLY_WRITE))
    monkeypatch.setattr(safety_config, "read_limit_for",
                        lambda action: READ_CAPS.get(action, 0))


def find_action(body: dict, name: str) -> dict:
    (action,) = [a for a in body["accounts"][0]["actions"] if a["action"] == name]
    return action


# --- 1. exact JSON against hand-computed counters ------------------------------------
async def test_exact_json_against_manually_computed_counters(caps):
    """used=1/cap=3 per account, used=4/cap=9 aggregate — the numbers asserted to the
    last field, including `binding`/`remaining` picking the SMALLER ceiling."""
    redis = FakeRedis()
    redis.seed(w_acct_key(ACCOUNT_ID, "joins_per_day"), 1, ttl=61234)
    redis.seed(w_api_key(API_ID, "public_reply", "joins_per_day"), 4, ttl=50000)

    async with make_client([(StubAccount(ACCOUNT_ID), StubCred())], redis) as client:
        resp = await client.get("/v1/limits", params={"account_ids": "12"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["missing"] == []
    assert len(body["accounts"]) == 1
    # generated_at: ISO-8601 UTC from the Clock
    datetime.fromisoformat(body["generated_at"])
    assert body["generated_at"].endswith("+00:00")

    account = body["accounts"][0]
    assert account["account_id"] == ACCOUNT_ID
    assert account["use_case"] == "public_reply"
    assert account["api_credential_id"] == CRED_ID
    assert account["cap_profile"] == "conservative"
    assert [a["action"] for a in account["actions"]] == expected_action_order()
    assert len(account["actions"]) == 11

    joins = find_action(body, "joins_per_day")
    assert joins == {
        "action": "joins_per_day",
        "kind": "write",
        "per_account": {
            "cap": 3, "used": 1, "remaining": 2, "resets_in_seconds": 61234,
        },
        "aggregate": {
            "scope": "api_credential",
            "api_credential_id": CRED_ID,
            "use_case": "public_reply",
            "account_count": ACCOUNT_COUNT,
            "cap": 9,          # round(0.6 × 3 × 5)
            "used": 4,
            "remaining": 5,
            "resets_in_seconds": 50000,
        },
        # per-account ceiling (2) is the smaller → it binds; remaining = min(2, 5)
        "binding": "per_account",
        "remaining": 2,
    }

    # The rule's other side: when the AGGREGATE ceiling is the smaller one, it binds.
    # Account 13 is fresh (per-account remaining 3), the shared aggregate is one unit
    # from its cap.
    redis.seed(w_api_key(API_ID, "public_reply", "joins_per_day"), 8, ttl=50000)
    async with make_client([(StubAccount(13), StubCred())], redis) as client:
        resp = await client.get("/v1/limits", params={"account_ids": "13"})

    assert resp.status_code == 200
    joins13 = find_action(resp.json(), "joins_per_day")
    assert joins13["per_account"] == {
        "cap": 3, "used": 0, "remaining": 3, "resets_in_seconds": None,
    }
    assert joins13["aggregate"]["remaining"] == 1
    assert joins13["binding"] == "aggregate"
    assert joins13["remaining"] == 1


# --- 2. aggregate only for the three sensitive writes --------------------------------
async def test_aggregate_is_null_for_reactions_and_all_seven_reads(caps):
    redis = FakeRedis()
    async with make_client([(StubAccount(ACCOUNT_ID), StubCred())], redis) as client:
        resp = await client.get("/v1/limits", params={"account_ids": "12"})

    actions = resp.json()["accounts"][0]["actions"]
    without_aggregate = [a["action"] for a in actions if a["aggregate"] is None]
    assert without_aggregate == ["reactions_per_day", *READ_CAPS.keys()], (
        "aggregate должен быть null ровно у reactions_per_day и семи чтений")
    for a in actions:
        if a["aggregate"] is None:
            assert a["binding"] == "per_account"
            assert a["remaining"] == a["per_account"]["remaining"]


# --- 3. resets_in_seconds == key TTL; absent key → used 0 / null ----------------------
async def test_resets_is_ttl_and_missing_key_reads_zero(caps):
    redis = FakeRedis()
    redis.seed(read_key("get_chat_info", ACCOUNT_ID), 2, ttl=777)

    async with make_client([(StubAccount(ACCOUNT_ID), StubCred())], redis) as client:
        resp = await client.get("/v1/limits", params={"account_ids": "12"})

    info = find_action(resp.json(), "get_chat_info")
    assert info["per_account"] == {
        "cap": 200, "used": 2, "remaining": 198, "resets_in_seconds": 777,
    }
    # get_dialogs was never spent: no key → used 0, remaining = cap, resets null.
    dialogs = find_action(resp.json(), "get_dialogs")
    assert dialogs["per_account"] == {
        "cap": 50, "used": 0, "remaining": 50, "resets_in_seconds": None,
    }


# --- 4. unknown id → 200 + missing, others unaffected --------------------------------
async def test_unknown_id_lands_in_missing_with_200(caps):
    redis = FakeRedis()
    async with make_client([(StubAccount(ACCOUNT_ID), StubCred())], redis) as client:
        resp = await client.get("/v1/limits", params={"account_ids": "12,999"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["missing"] == [999]
    assert [a["account_id"] for a in body["accounts"]] == [ACCOUNT_ID]

    # No parameter → whole instance, `missing` present and empty.
    async with make_client([(StubAccount(ACCOUNT_ID), StubCred())], redis) as client:
        resp = await client.get("/v1/limits")

    assert resp.status_code == 200
    assert resp.json()["missing"] == []


# --- 5. key shapes byte-for-byte against the worker's f-strings -----------------------
async def test_key_shapes_match_worker_forms_byte_for_byte(caps):
    """The keys the route peeks MUST be the keys the worker writes — built here from
    the worker's own f-strings (budget.py:108-116, base_task.py:231, E6 shape), so a
    rename on either side breaks this test instead of silently showing zeros."""
    redis = FakeRedis()
    async with make_client([(StubAccount(ACCOUNT_ID), StubCred())], redis) as client:
        resp = await client.get("/v1/limits", params={"account_ids": "12"})

    assert resp.status_code == 200
    expected = set()
    for action in expected_action_order()[:4]:  # WRITE_BUDGET_ACTION.values()
        expected.add(w_acct_key(ACCOUNT_ID, action))
    for action in SENSITIVE:
        expected.add(w_api_key(API_ID, "public_reply", action))
    for action in READ_CAPS:
        expected.add(read_key(action, ACCOUNT_ID))

    # 11 per-account + 3 deduplicated aggregates; not one extra key read. `requested`
    # records every pipeline COMMAND (GET and TTL each append), so distinct keys is the
    # key-set invariant; the 2× total is the contract's own cost model (§3: GET+TTL per
    # key → 2·(11·N + 3·A) commands, one pipeline for the whole snapshot).
    assert len(set(redis.requested)) == 14, sorted(set(redis.requested))
    assert set(redis.requested) == expected, (
        f"ручка читает чужие ключи: {sorted(set(redis.requested) ^ expected)}")
    assert len(redis.requested) == 28, (
        f"ожидался один GET+TTL на каждый из 14 ключей, команд: {len(redis.requested)}")


# --- 6. cap 0 stays visible ------------------------------------------------------------
async def test_zero_cap_invites_present_with_zero_remaining(caps):
    """invites_per_day is forbidden for public_reply (cap 0): the action still appears
    — with zeros and its aggregate block (whose formula gives 0 too)."""
    redis = FakeRedis()
    async with make_client([(StubAccount(ACCOUNT_ID), StubCred())], redis) as client:
        resp = await client.get("/v1/limits", params={"account_ids": "12"})

    invites = find_action(resp.json(), "invites_per_day")
    assert invites["kind"] == "write"
    assert invites["per_account"] == {
        "cap": 0, "used": 0, "remaining": 0, "resets_in_seconds": None,
    }
    assert invites["binding"] == "per_account"
    assert invites["aggregate"] == {
        "scope": "api_credential",
        "api_credential_id": CRED_ID,
        "use_case": "public_reply",
        "account_count": ACCOUNT_COUNT,
        "cap": 0,  # max(0, round(0.6 × 0 × 5))
        "used": 0,
        "remaining": 0,
        "resets_in_seconds": None,
    }
    assert invites["remaining"] == 0


# --- errors pinned in §2 ---------------------------------------------------------------
async def test_non_integer_account_ids_returns_422(caps):
    redis = FakeRedis()
    async with make_client([(StubAccount(ACCOUNT_ID), StubCred())], redis) as client:
        resp = await client.get("/v1/limits", params={"account_ids": "1,abc"})

    assert resp.status_code == 422
    assert resp.json() == {"detail": "account_ids: ожиданы целые числа"}


async def test_redis_unavailable_returns_503_without_partial_body(caps):
    """Caps without spent would be a витрина lying in both halves — Redis being down
    fails the whole request, no partial answer."""
    redis = FakeRedis()
    redis.fail = True
    async with make_client([(StubAccount(ACCOUNT_ID), StubCred())], redis) as client:
        resp = await client.get("/v1/limits", params={"account_ids": "12"})

    assert resp.status_code == 503
    assert resp.json() == {"detail": "redis unavailable"}
