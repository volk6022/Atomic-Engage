"""Worker-path schedule via the deterministic stub store (US4 follow-up).

No DB table/migration: the weekly schedule is a pure function of (account.id, virtual
week), so every stateless worker derives the SAME schedule for an account without any
persistence. ``WorkingHoursGuard`` (which base_task already calls) delegates to this
store for id-bearing accounts and keeps the flat-window behaviour for id-less mocks.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.clock import Clock, set_clock
from app.services.schedule_service import ScheduleService, get_schedule_service
from app.services.working_hours import WorkingHoursGuard


class _Proxy:
    def __init__(self, tz_offset):
        self.tz_offset = tz_offset


class _Account:
    def __init__(self, account_id, tz_offset=0, work_start=8, work_end=22):
        self.id = account_id
        self.proxy = _Proxy(tz_offset)
        self.work_start = work_start
        self.work_end = work_end


@pytest.fixture(autouse=True)
def _reset_clock_and_store():
    set_clock(Clock(time_scale=1.0))
    # Fresh store each test so determinism isn't masked by a stale cache.
    import app.services.schedule_service as ss
    ss._schedule_service = None
    yield


def test_deterministic_schedule_is_stable_for_same_account():
    svc = ScheduleService(clock=Clock(1.0))
    a = _Account(424242)
    s1 = svc.deterministic_schedule(a)
    # A second instance (a "different worker") must derive the identical schedule.
    svc2 = ScheduleService(clock=Clock(1.0))
    s2 = svc2.deterministic_schedule(_Account(424242))
    assert s1.rest_days == s2.rest_days
    assert (s1.start_hour, s1.end_hour) == (s2.start_hour, s2.end_hour)


def test_distribution_across_accounts_has_second_rest_day_30pct():
    svc = ScheduleService(clock=Clock(1.0))
    two = sum(1 for i in range(2000) if len(svc.deterministic_schedule(_Account(i)).rest_days) == 2)
    assert abs(two / 2000 - 0.30) < 0.06


def test_schedule_honours_account_work_hours():
    svc = ScheduleService(clock=Clock(1.0))
    a = _Account(7, work_start=8, work_end=22)
    sched = svc.deterministic_schedule(a)
    assert sched.start_hour == 8
    assert sched.end_hour == 22


def test_full_day_window_disables_scheduling():
    """work_start=0/work_end=24 means 'always on, no rest days' (test-account convention)."""
    svc = ScheduleService(clock=Clock(1.0))
    a = _Account(99, work_start=0, work_end=24)
    sched = svc.deterministic_schedule(a)
    assert sched.rest_days == set()
    # Active at any hour incl. deep night, and never deferred.
    midnight = datetime(2026, 6, 1, 3, tzinfo=timezone.utc)
    assert svc.should_be_active(a, midnight) is True


def _monday_of_current_week(clock: Clock) -> datetime:
    now = clock.now()
    return (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def test_working_hours_guard_delegates_for_id_accounts():
    clock = Clock(1.0)
    set_clock(clock)
    guard = WorkingHoursGuard()
    a = _Account(515151, tz_offset=0, work_start=8, work_end=22)
    sched = get_schedule_service().deterministic_schedule(a)

    monday = _monday_of_current_week(clock)
    rest = next(iter(sched.rest_days))
    work = next(d for d in range(7) if d not in sched.rest_days)

    # On a rest day, midday -> not allowed (deferred).
    allowed_rest, defer = guard.check(a, monday + timedelta(days=rest, hours=12))
    assert allowed_rest is False
    assert defer is not None
    # On a working day, inside the 8-22 window -> allowed.
    allowed_work, _ = guard.check(a, monday + timedelta(days=work, hours=12))
    assert allowed_work is True


def test_working_hours_guard_flat_fallback_for_idless_account():
    """Id-less mock (as in test_working_hours) keeps the pure flat-window behaviour."""
    guard = WorkingHoursGuard()

    class _Mock:
        work_start = 9
        work_end = 22
        proxy = _Proxy(10800)

    allowed, _ = guard.check(_Mock(), datetime(2026, 5, 4, 12, 0, 0))
    assert allowed is True
    blocked, defer = guard.check(_Mock(), datetime(2026, 5, 4, 3, 0, 0))
    assert blocked is False and defer is not None
