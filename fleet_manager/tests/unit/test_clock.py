"""Unit tests for the time-scale Clock (FR-300/301/302, SC-003).

These run without any infrastructure: pure arithmetic + a short real sleep to
prove virtual time advances by the scale factor (verification *by elapsed time*).
"""
import asyncio
import time
from datetime import datetime, timezone

from app.core.clock import Clock


def test_scale_one_is_wallclock_noop():
    """At TIME_SCALE=1 the Clock is a no-op vs wall clock (SC-003)."""
    c = Clock(time_scale=1.0)
    delta = abs((c.now() - datetime.now(timezone.utc)).total_seconds())
    assert delta < 1.0
    assert c.scaled_ttl(86400) == 86400
    assert c.scaled_ttl(300) == 300
    assert c.scaled_sleep_seconds(300) == 300.0


def test_scaled_ttl_compresses_and_floors():
    c = Clock(time_scale=48.0)
    assert c.scaled_ttl(86400) == 1800   # virtual 24h -> 30 real min
    assert c.scaled_ttl(300) == 6        # proxy health 5 min -> ~6 s
    # Floor: a TTL that would round below 1 s clamps to 1 (edge case in spec).
    assert c.scaled_ttl(10) == max(1, round(10 / 48))
    assert c.scaled_ttl(1) == 1


def test_scaled_sleep_seconds():
    c = Clock(time_scale=48.0)
    assert c.scaled_sleep_seconds(300) == 300 / 48  # 6.25 real s for virtual 300


def test_now_advances_at_scale_by_elapsed_time():
    """Virtual time advances ~48x real elapsed time (proven by sleeping)."""
    c = Clock(time_scale=48.0)
    t0 = c.now()
    time.sleep(0.5)
    virtual_delta = (c.now() - t0).total_seconds()
    # 0.5 real s * 48 ~= 24 virtual s; allow generous tolerance for scheduler jitter.
    assert 18 <= virtual_delta <= 36


def test_async_sleep_uses_scaled_real_duration():
    c = Clock(time_scale=48.0)

    async def _run():
        start = time.monotonic()
        await c.sleep(48)  # virtual 48 s -> 1.0 real s
        return time.monotonic() - start

    real = asyncio.run(_run())
    assert 0.7 <= real <= 1.4


def test_from_env_defaults_to_one(monkeypatch):
    monkeypatch.delenv("TIME_SCALE", raising=False)
    assert Clock.from_env().time_scale == 1.0
    monkeypatch.setenv("TIME_SCALE", "48")
    assert Clock.from_env().time_scale == 48.0
