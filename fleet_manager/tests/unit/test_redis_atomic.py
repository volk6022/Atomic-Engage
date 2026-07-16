"""Atomic rate-limit counter + scaled TTL (FR-350/301, SC-004).

Requires a real Redis (the `redis_client` fixture skips honestly when absent). The Lua
INCR+EXPIRE is atomic, so a counter can NEVER exist without a TTL — there is no longer
a crash window between the two commands.
"""
import asyncio

import pytest

from app.core.clock import Clock


@pytest.mark.asyncio
async def test_increment_always_carries_ttl(redis_client):
    from app.db import redis_client as rc

    await redis_client.delete("rate:atomic:k1")
    count = await rc.rate_limit_increment(redis_client, "atomic:k1", ttl=86400)
    assert count == 1
    ttl = await redis_client.ttl("rate:atomic:k1")
    # A TTL-less (-1) or missing (-2) key is exactly the eternal-counter bug we fixed.
    assert 0 < ttl <= 86400


@pytest.mark.asyncio
async def test_ttl_is_scaled_by_clock(redis_client):
    from app.db import redis_client as rc

    await redis_client.delete("rate:atomic:k2")
    await rc.rate_limit_increment(
        redis_client, "atomic:k2", ttl=86400, clock=Clock(time_scale=48.0)
    )
    ttl = await redis_client.ttl("rate:atomic:k2")
    assert 0 < ttl <= 1900  # ~1800 s real for a 24 h virtual budget


@pytest.mark.asyncio
async def test_concurrent_increments_are_consistent_and_bounded(redis_client):
    from app.db import redis_client as rc

    await redis_client.delete("rate:atomic:k3")
    results = await asyncio.gather(
        *[rc.rate_limit_increment(redis_client, "atomic:k3", ttl=600) for _ in range(50)]
    )
    assert sorted(results) == list(range(1, 51))   # no lost/duplicated increments
    assert 0 < await redis_client.ttl("rate:atomic:k3") <= 600


@pytest.mark.asyncio
async def test_peek_does_not_mutate(redis_client):
    from app.db import redis_client as rc

    await redis_client.delete("rate:atomic:k4")
    assert await rc.rate_limit_peek(redis_client, "atomic:k4") == 0
    await rc.rate_limit_increment(redis_client, "atomic:k4", ttl=600)
    assert await rc.rate_limit_peek(redis_client, "atomic:k4") == 1
    assert await rc.rate_limit_peek(redis_client, "atomic:k4") == 1  # unchanged
