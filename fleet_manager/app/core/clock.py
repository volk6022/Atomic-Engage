"""Time-scale Clock (feature 003, FR-300/301/302).

A single injectable time source so the entire ban-safety cycle can be observed in
compressed real time (virtual 24 h = real 30 min at ``TIME_SCALE=48``) — and so a
test verifies the schedulers *by elapsed time*, not by a frozen/mocked instant.

The whole subsystem reads time through three choke points:

* :meth:`Clock.now` — every safety wall-clock comparison.
* :meth:`Clock.scaled_ttl` — every safety Redis TTL.
* :meth:`Clock.sleep` / :meth:`Clock.scaled_sleep_seconds` — every humanizer sleep.

**Invariant (SC-003):** at ``time_scale == 1.0`` all three are exact no-ops versus
wall clock, so production behaviour is unchanged and acceleration is test-only.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone


class Clock:
    """Virtual-time source advancing at ``time_scale`` × real time.

    ``now() = t0_virtual + (real_now - t0_real) * time_scale``.
    """

    __slots__ = ("time_scale", "_t0_real_monotonic", "_t0_virtual")

    def __init__(
        self,
        time_scale: float = 1.0,
        *,
        t0_virtual: datetime | None = None,
    ) -> None:
        if time_scale <= 0:
            raise ValueError("time_scale must be > 0")
        self.time_scale = float(time_scale)
        # Anchor virtual time to "now" so that at scale 1.0 now() == wall clock.
        self._t0_virtual = (t0_virtual or datetime.now(timezone.utc)).astimezone(
            timezone.utc
        )
        # Use a monotonic anchor for elapsed-time math (immune to wall-clock steps).
        self._t0_real_monotonic = time.monotonic()

    @classmethod
    def from_env(cls) -> "Clock":
        """Build from the ``TIME_SCALE`` env var (default 1.0 — production)."""
        raw = os.environ.get("TIME_SCALE", "1.0")
        try:
            scale = float(raw)
        except (TypeError, ValueError):
            scale = 1.0
        return cls(time_scale=scale)

    def now(self) -> datetime:
        """Current virtual time (tz-aware UTC)."""
        real_elapsed = time.monotonic() - self._t0_real_monotonic
        return self._t0_virtual + timedelta(seconds=real_elapsed * self.time_scale)

    def scaled_ttl(self, base_seconds: int) -> int:
        """Compress a virtual TTL into real seconds, floored at 1 s.

        A 24 h (86400 s) virtual budget expires in ~1800 real s at 48×. The floor
        keeps even the shortest TTL ≥ 1 s so Redis can still expire it reliably.
        """
        return max(1, round(base_seconds / self.time_scale))

    def scaled_sleep_seconds(self, base_seconds: float) -> float:
        """Real seconds to sleep so a *virtual* ``base_seconds`` elapses."""
        return base_seconds / self.time_scale

    async def sleep(self, base_seconds: float) -> None:
        """Async-sleep for the real duration matching a virtual ``base_seconds``."""
        await asyncio.sleep(self.scaled_sleep_seconds(base_seconds))


_clock: Clock | None = None


def get_clock() -> Clock:
    """Process-wide Clock, built once from ``TIME_SCALE``.

    Workers, watchers, and the gateway share this instance; tests inject their own
    accelerated Clock instead of relying on this singleton.
    """
    global _clock
    if _clock is None:
        _clock = Clock.from_env()
    return _clock


def set_clock(clock: Clock) -> None:
    """Override the process-wide Clock (used by the accelerated test harness)."""
    global _clock
    _clock = clock
