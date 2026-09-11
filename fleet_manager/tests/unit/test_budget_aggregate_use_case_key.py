"""The api_id aggregate budget must be keyed per use_case (E6).

One api_id serves accounts running different use_cases. While the aggregate key
was `budget:api:{api_id}:{action}` those accounts shared a single counter whose
cap was recomputed from whichever use_case happened to be calling — so a
`cold_dm` account could spend (under its own, larger cap) the very counter a
`public_reply` fleet depends on. E6 scopes the key:
`budget:api:{api_id}:{use_case}:{action}`.

Two use_cases on one api_id must therefore spend two independent counters, each
capped by its own use_case's numbers; a spend or a refusal under one must not
touch the other, and the old use_case-less key must no longer exist.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import budget


class FakeRedis:
    """Minimal Redis exposing exactly the consume-script contract.

    Same contract the atomicity suite pins down (see
    test_budget_consume_is_atomic.FakeRedis): `eval` is atomic, consumes from
    every key only when ALL have headroom, returns `[refusal_index]` when key
    `refusal_index` refuses and `[0, remaining...]` after a full consume. Here
    the point is not concurrency but the key namespacing, so a plain
    implementation of that contract is enough.
    """

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttl: dict[str, int] = {}

    async def eval(self, script, numkeys, *args):
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])
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


@pytest.fixture()
def caps(monkeypatch):
    """Two use_cases on one api_id, each with its own join cap.

    With two members the aggregate caps are: public_reply
    `max(3, round(0.6 × 3 × 2)) = 4`, cold_dm `max(4, round(0.6 × 4 × 2)) = 5`.
    """
    table = {"public_reply": {"joins_per_day": 3}, "cold_dm": {"joins_per_day": 4}}
    monkeypatch.setattr(budget.safety_config, "rate_limit_for_profile",
                        lambda profile, use_case: dict(table[use_case]))
    monkeypatch.setattr(budget.safety_config, "get_premium_ceilings", lambda: {})


async def _consume(redis, account_id: int, use_case: str):
    return await budget.check_and_consume(
        redis, None,
        account_id=account_id, api_id=2040, proxy_subnet=f"proxy:{account_id}",
        action="joins_per_day", use_case=use_case,
        cap_profile="conservative", is_premium=False,
        api_id_member_count=2, subnet_member_count=1)


def test_two_use_cases_on_one_api_id_keep_separate_aggregates(caps):
    """Same api_id, two use_cases: two counters, two caps, no cross-debit."""
    redis = FakeRedis()

    async def go():
        # Drain the public_reply aggregate (cap 4): the fifth ask has per-account
        # headroom on account 1, so the refusal is the aggregate's.
        pr = [await _consume(redis, a, "public_reply") for a in (1, 2, 1, 2, 1)]
        # cold_dm on the SAME api_id then spends its own aggregate (cap 5); its
        # counter must start from zero, not from public_reply's four units.
        dm = [await _consume(redis, a, "cold_dm") for a in (11, 12, 11, 12, 11, 12)]
        # One more cold_dm ask is refused; it must not debit public_reply either.
        cross = await _consume(redis, 12, "cold_dm")
        return pr, dm, cross

    pr, dm, cross = asyncio.run(go())

    assert [r.allowed for r in pr] == [True, True, True, True, False]
    assert pr[4].binding == "aggregate"
    assert redis.store["rate:budget:api:2040:public_reply:joins_per_day"] == 4, (
        f"счётчик public_reply = {redis.store['rate:budget:api:2040:public_reply:joins_per_day']} при потолке 4")

    assert [r.allowed for r in dm] == [True, True, True, True, True, False]
    assert dm[5].binding == "aggregate"
    assert redis.store["rate:budget:api:2040:cold_dm:joins_per_day"] == 5, (
        f"счётчик cold_dm = {redis.store['rate:budget:api:2040:cold_dm:joins_per_day']} при потолке 5 "
        "— в него попали чужие единицы или его потолок взят из другого use_case")

    assert cross.allowed is False and cross.binding == "aggregate"
    assert redis.store["rate:budget:api:2040:public_reply:joins_per_day"] == 4, (
        "отказ cold_dm списал единицу из счётчика public_reply")

    # The old, use_case-less key must no longer exist.
    assert "rate:budget:api:2040:joins_per_day" not in redis.store, (
        "агрегат по api_id всё ещё пишется в старый ключ без use_case")
