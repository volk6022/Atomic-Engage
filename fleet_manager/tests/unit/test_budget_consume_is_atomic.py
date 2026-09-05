"""The budget check and its consume must be one atomic step (FR-340/341/342).

Measured on prod, not imagined: on 2026-09-05 the fleet's aggregate join counter,
`rate:budget:api:2040:joins_per_day`, stood at **10** against a cap of **9**
(`round(0.6 × 3 × 5)`). Nothing was misconfigured — the enforcement simply cannot
hold under concurrency, because `check_and_consume` reads every counter first
(`rate_limit_peek`), then decides, then increments. Each increment is atomic on its
own; the decision that authorises them is not. Two workers on different accounts read
the same value, both see headroom, and both spend it. The overshoot equals the number
of workers acting at once — and Radar orders joins in exactly that shape, one worker
per account.

This is the same family as the struck-off "cap 20, allowed 25" finding, except this
one is real and was found in live counters rather than in a test fixture.

The fix has to keep the *stricter-of* semantics intact: an action is allowed only if
the per-account budget **and** every applicable aggregate budget have headroom, and
nothing is spent when any of them refuses. Partial consumption is the other failure
mode to avoid — spending the per-account unit and then refusing on the aggregate would
burn an account's daily budget for an action it never performed. That is not
hypothetical either: on 05.09 account 2 spent all three of its daily joins without
joining anything, because the budget was debited before a pause that later timed out.

These tests drive Redis directly through a fake that honours only what the real
server guarantees — single-command atomicity and whole-script atomicity — so a
solution that "works" by being lucky about ordering fails here.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import budget


class FakeRedis:
    """Minimal Redis with the one guarantee that matters: a script runs alone.

    Every command yields control (`await asyncio.sleep(0)`) so interleaving is not
    merely possible but certain — the point is to make the race deterministic rather
    than hope a timing bug shows up. `eval` holds a lock for the whole script, which
    is exactly what a real server does with a Lua script.
    """

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttl: dict[str, int] = {}
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
            keys = list(args[:numkeys])
            argv = list(args[numkeys:])
            return await self._run(script, keys, argv)

    async def _run(self, script, keys, argv):
        """Interpret the two scripts this module is allowed to use.

        A real Lua interpreter is not the point; the guarantee is. Anything else
        raises, so a rewrite that quietly starts issuing plain commands instead of a
        script is caught here rather than in production counters.
        """
        await asyncio.sleep(0)
        if "INCR" in script and len(keys) == 1 and len(argv) == 1:
            self.store[keys[0]] = self.store.get(keys[0], 0) + 1
            if self.store[keys[0]] == 1:
                self.ttl[keys[0]] = int(argv[0])
            return self.store[keys[0]]
        # The consume script's contract: `{i}` names the 1-based index of the first
        # counter that refused, `{0, remaining...}` reports what is left after a
        # successful consume. Redis hands a Lua table back as a list, so a list is
        # what the fake returns.
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
    """One account cap of 3, five members on the api_id — the live fleet's shape.

    Aggregate cap is then `max(3, round(0.6 × 3 × 5)) = 9`, the number that was
    pierced on prod.
    """
    monkeypatch.setattr(budget.safety_config, "rate_limit_for_profile",
                        lambda profile, use_case: {"joins_per_day": 3})
    monkeypatch.setattr(budget.safety_config, "get_premium_ceilings", lambda: {})


async def _consume(redis, account_id: int):
    return await budget.check_and_consume(
        redis, None,
        account_id=account_id, api_id=2040, proxy_subnet=f"proxy:{account_id}",
        action="joins_per_day", use_case="public_reply",
        cap_profile="conservative", is_premium=False,
        api_id_member_count=5, subnet_member_count=1)


def test_aggregate_cap_holds_against_concurrent_accounts(caps):
    """Nine allowed, the tenth refused — however many workers ask at once.

    Five accounts × three joins each is fifteen requests; the aggregate lets nine
    through. Before the fix this counter reached ten on the real fleet.
    """
    redis = FakeRedis()

    async def go():
        results = await asyncio.gather(
            *[_consume(redis, account_id)
              for account_id in (1, 2, 3, 4, 5) for _ in range(3)])
        return results

    results = asyncio.run(go())

    allowed = [r for r in results if r.allowed]
    assert len(allowed) == 9, (
        f"пропущено {len(allowed)} при потолке 9 — совокупный бюджет пробит")
    assert redis.store["rate:budget:api:2040:joins_per_day"] == 9, (
        f"счётчик {redis.store['rate:budget:api:2040:joins_per_day']} при потолке 9")
    assert all(r.binding == "aggregate" for r in results if not r.allowed)


def test_nothing_is_spent_when_the_aggregate_refuses(caps):
    """A refusal must not debit the per-account counter.

    Otherwise an account burns its daily budget on an action it never performed —
    the exact shape of the 05.09 loss, where account 2 spent three joins and joined
    nothing.
    """
    redis = FakeRedis()

    async def go():
        # Fill the aggregate with four other accounts (nine units), then ask again.
        await asyncio.gather(*[_consume(redis, a) for a in (1, 2, 3) for _ in range(3)])
        return await _consume(redis, 4)

    decision = asyncio.run(go())

    assert decision.allowed is False
    assert decision.binding == "aggregate"
    assert redis.store.get("rate:budget:acct:4:joins_per_day", 0) == 0, (
        "отказ совокупного бюджета списал поаккаунтную единицу")


def test_per_account_cap_still_binds_first(caps):
    """The per-account cap keeps its own meaning: three joins for one account, then
    a refusal that names the account — not the fleet."""
    redis = FakeRedis()

    async def go():
        return await asyncio.gather(*[_consume(redis, 1) for _ in range(5)])

    results = asyncio.run(go())

    assert len([r for r in results if r.allowed]) == 3
    refused = [r for r in results if not r.allowed]
    assert all(r.binding == "per_account" for r in refused), (
        f"после трёх вступлений связывает аккаунт, а не флот: {refused}")


def test_every_counter_gets_an_expiry(caps):
    """A counter without a TTL wedges an account over-cap forever — the reason the
    increment became a script in the first place (FR-350)."""
    redis = FakeRedis()

    asyncio.run(_consume(redis, 1))

    for key in redis.store:
        assert key in redis.ttl and redis.ttl[key] > 0, f"счётчик {key} без TTL"


def test_reads_are_not_aggregate_budgeted(caps, monkeypatch):
    """Only SENSITIVE_ACTIONS carry the fleet budget; reads have their own
    per-account limits and must not be throttled by the fleet's join cap."""
    monkeypatch.setattr(budget.safety_config, "rate_limit_for_profile",
                        lambda profile, use_case: {"get_chat_history": 2000})
    redis = FakeRedis()

    async def go():
        return await budget.check_and_consume(
            redis, None, account_id=1, api_id=2040, proxy_subnet="proxy:1",
            action="get_chat_history", use_case="public_reply",
            cap_profile="conservative", is_premium=False,
            api_id_member_count=5, subnet_member_count=1)

    decision = asyncio.run(go())

    assert decision.allowed is True
    assert "rate:budget:api:2040:get_chat_history" not in redis.store
