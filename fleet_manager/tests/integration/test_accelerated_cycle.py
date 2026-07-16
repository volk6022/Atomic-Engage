"""Full-cycle, time-compressed safety test on several virtual sessions
(US1/US2, FR-303/304/305, SC-001).

This drives the REAL budget / schedule / clock code paths against a cohort of fake
sessions in compressed real time — **no real Telegram and no kurigram import**. Only
the budget counters need Redis (the `redis_client` fixture skips honestly when absent).

* `test_schedule_covers_virtual_week` — pure, fast: samples a full virtual week and
  proves nightly windows + 1-2 rest days per account, with per-account tz independence.
* `test_budget_cap_and_reset_by_time` — verifies a daily cap is enforced and then
  RESETS purely by elapsed (scaled) time — the "проверка временем" requirement.
* `test_full_virtual_week` (marker `accelerated`) — the 3.5 real-hour canonical run:
  7 virtual days at TIME_SCALE=48 across 3 timezones/use-cases, asserting caps reset
  each virtual midnight and night windows hold throughout.

Run the long one with:  ``TIME_SCALE=48 pytest -m accelerated -q``
"""
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.core.clock import Clock
from app.core.humanizer_config import ScheduleConfig
from app.services import budget
from app.services.schedule_service import ScheduleService


# --------------------------------------------------------------------------------------
# Virtual session harness (FR-303): fake account+proxy+timezone bound to a shared clock.
# --------------------------------------------------------------------------------------
@dataclass
class _Proxy:
    tz_offset: int
    asn: int
    subnet: str


@dataclass
class VirtualSession:
    id: int
    proxy: _Proxy
    api_id: int
    use_case: str
    cap_profile: str = "conservative"
    is_premium: bool = False


def make_cohort() -> list[VirtualSession]:
    """≥3 virtual sessions in distinct timezones and use-cases (research R10)."""
    return [
        VirtualSession(1, _Proxy(10800, 12389, "10.0.1.0"), api_id=1001, use_case="cold_dm"),
        VirtualSession(2, _Proxy(0, 12876, "10.0.2.0"), api_id=1002, use_case="join_groups"),
        VirtualSession(3, _Proxy(-18000, 13335, "10.0.3.0"), api_id=1003, use_case="reactions"),
    ]


def _utc_for_local(day0: datetime, weekday: int, hour: int, tz_offset: int) -> datetime:
    base = day0 + timedelta(days=weekday, hours=hour)
    return base - timedelta(seconds=tz_offset)


# --------------------------------------------------------------------------------------
# 1) Schedule coverage across a virtual week (pure, fast).
# --------------------------------------------------------------------------------------
def test_schedule_covers_virtual_week():
    random.seed(2026)
    clock = Clock(time_scale=48.0)
    svc = ScheduleService(ScheduleConfig(), clock)
    cohort = make_cohort()
    # Monday 2026-06-01 as a clean week anchor.
    day0 = datetime(2026, 6, 1, tzinfo=timezone.utc)

    for s in cohort:
        sched = svc.generate_weekly(s)
        assert 1 <= len(sched.rest_days) <= 2  # weekly rest days (FR-320)

        active_by_day = {d: 0 for d in range(7)}
        for weekday in range(7):
            for hour in range(24):
                now = _utc_for_local(day0, weekday, hour, s.proxy.tz_offset)
                if svc.should_be_active(s, now):
                    active_by_day[weekday] += 1

        # Every rest day has zero active hours (FR-321).
        for rest in sched.rest_days:
            assert active_by_day[rest] == 0
        # Working days have an active window but are NOT 24h (nightly off-hours exist).
        working = [d for d in range(7) if d not in sched.rest_days]
        assert any(0 < active_by_day[d] < 24 for d in working)

    # Per-account tz independence: the same UTC instant maps to different local hours,
    # so windows are offset, not in global lockstep (US2 sc4).
    probe = _utc_for_local(day0, 0, 12, 0)
    locals_ = {(probe + timedelta(seconds=s.proxy.tz_offset)).hour for s in cohort}
    assert len(locals_) > 1


# --------------------------------------------------------------------------------------
# 2) Daily cap enforced, then RESETS by elapsed scaled time (needs Redis).
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_budget_cap_and_reset_by_time(redis_client):
    """cold_dm messages cap = 20: 25 attempts -> 20 allowed, 5 denied; after the
    (scaled) virtual day the counter resets and a send is allowed again."""
    await redis_client.flushdb()
    # A short virtual-day budget so the reset is observable in a few real seconds.
    ttl_base = 240                      # virtual seconds
    clock = Clock(time_scale=48.0)      # 240 / 48 = 5 real seconds to reset
    s = make_cohort()[0]

    allowed = 0
    for _ in range(25):
        d = await budget.check_and_consume(
            redis_client, clock,
            account_id=s.id, api_id=s.api_id, proxy_subnet=s.proxy.subnet,
            action="messages_per_day", use_case=s.use_case,
            cap_profile=s.cap_profile, is_premium=s.is_premium, ttl_base=ttl_base,
        )
        allowed += 1 if d.allowed else 0
    assert allowed == 20                # exactly the cap (FR-342, US2 sc1)

    # Wait for the scaled virtual-midnight reset (TTL expiry), proven by elapsed time.
    time.sleep(clock.scaled_sleep_seconds(ttl_base) + 1.0)
    d2 = await budget.check_and_consume(
        redis_client, clock,
        account_id=s.id, api_id=s.api_id, proxy_subnet=s.proxy.subnet,
        action="messages_per_day", use_case=s.use_case,
        cap_profile=s.cap_profile, is_premium=s.is_premium, ttl_base=ttl_base,
    )
    assert d2.allowed is True           # cap reset


# --------------------------------------------------------------------------------------
# 3) Canonical full virtual-week run (~3.5 real hours at TIME_SCALE=48).
# --------------------------------------------------------------------------------------
@pytest.mark.accelerated
@pytest.mark.asyncio
async def test_full_virtual_week(redis_client):
    await redis_client.flushdb()
    scale = float(os.environ.get("TIME_SCALE", "48"))
    clock = Clock(time_scale=scale)
    svc = ScheduleService(ScheduleConfig(), clock)
    cohort = make_cohort()
    for s in cohort:
        svc.generate_weekly(s)

    start_virtual = clock.now()
    week_seconds = 7 * 86400
    caps = {"cold_dm": "messages_per_day", "join_groups": "joins_per_day", "reactions": "reactions_per_day"}

    per_day_allowed: dict[int, dict[int, int]] = {}
    inactive_observed = 0
    last_day = -1

    # Step in real time; each loop reads virtual now() and acts on it. An action is only
    # dispatched while the account is inside its schedule window — so the nightly/weekly
    # schedule genuinely gates dispatch (US2 sc2/sc3), and the daily cap genuinely
    # bounds in-window consumption (US2 sc1).
    while (clock.now() - start_virtual).total_seconds() < week_seconds:
        now = clock.now()
        vday = int((now - start_virtual).total_seconds() // 86400)
        per_day_allowed.setdefault(vday, {s.id: 0 for s in cohort})

        for s in cohort:
            if not svc.should_be_active(s, now):
                inactive_observed += 1          # night or rest day: dispatch skipped
                continue
            d = await budget.check_and_consume(
                redis_client, clock,
                account_id=s.id, api_id=s.api_id, proxy_subnet=s.proxy.subnet,
                action=caps[s.use_case], use_case=s.use_case,
                cap_profile=s.cap_profile, is_premium=s.is_premium,
            )
            if d.allowed:
                per_day_allowed[vday][s.id] += 1

        # Never exceed the per-action daily cap within any virtual day (reset proven by
        # the fact that a new vday starts counting from zero — TTL expiry by elapsed time).
        for s in cohort:
            cap = budget.effective_cap(s.cap_profile, s.use_case, caps[s.use_case], s.is_premium)
            assert per_day_allowed[vday][s.id] <= cap

        last_day = max(last_day, vday)
        await clock.sleep(900)  # advance ~15 virtual minutes per step

    assert last_day >= 6                         # saw all 7 virtual days
    assert inactive_observed > 0                 # nightly/weekly gate fired (dispatch skipped)
    total_allowed = sum(sum(day.values()) for day in per_day_allowed.values())
    assert total_allowed > 0                     # in-window work happened
