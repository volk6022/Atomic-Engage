"""Unit tests for the weekly schedule / presence / DM-override (FR-320-323)."""
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.core.clock import Clock
from app.core.humanizer_config import ScheduleConfig
from app.services.schedule_service import ScheduleService


class _Proxy:
    def __init__(self, tz_offset):
        self.tz_offset = tz_offset


class _Account:
    def __init__(self, account_id=1, tz_offset=0):
        self.id = account_id
        self.proxy = _Proxy(tz_offset)


@pytest.fixture
def svc():
    return ScheduleService(ScheduleConfig(), Clock(time_scale=1.0))


def test_generate_weekly_rest_days_and_window_bounds(svc):
    random.seed(0)
    cfg = svc.cfg
    for _ in range(50):
        sch = svc.generate_weekly(_Account())
        assert 1 <= len(sch.rest_days) <= 2
        assert cfg.work_start_min_h <= sch.start_hour <= cfg.work_start_max_h
        assert cfg.work_end_min_h <= sch.end_hour <= cfg.work_end_max_h
        assert sch.start_minute in cfg.minute_granularity
        assert sch.end_minute in cfg.minute_granularity


def test_second_rest_day_probability_roughly_30pct(svc):
    random.seed(42)
    two = sum(1 for _ in range(2000) if len(svc.generate_weekly(_Account()).rest_days) == 2)
    assert abs(two / 2000 - 0.30) < 0.06


def _utc_for_local(weekday: int, hour: int, tz_offset: int) -> datetime:
    """A UTC instant whose local (offset-applied) time is the given weekday/hour."""
    # 2026-06-01 is a Monday (weekday 0). Build the local time, then subtract offset.
    base = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(days=weekday, hours=hour)
    return base - timedelta(seconds=tz_offset)


def test_should_be_active_inside_window_working_day(svc):
    random.seed(1)
    acct = _Account(tz_offset=10800)  # UTC+3
    sch = svc.generate_weekly(acct)
    working_day = next(d for d in range(7) if d not in sch.rest_days)
    now = _utc_for_local(working_day, (sch.start_hour + sch.end_hour) // 2, acct.proxy.tz_offset)
    assert svc.should_be_active(acct, now) is True
    assert svc.presence(acct, now) == "online"


def test_should_be_active_false_at_night_and_on_rest_day(svc):
    random.seed(1)
    acct = _Account(tz_offset=10800)
    sch = svc.generate_weekly(acct)
    working_day = next(d for d in range(7) if d not in sch.rest_days)
    # 03:00 local -> before the 9-10 window opens.
    night = _utc_for_local(working_day, 3, acct.proxy.tz_offset)
    assert svc.should_be_active(acct, night) is False
    assert svc.presence(acct, night) == "offline"
    assert svc.next_open(acct, night) is not None

    rest_day = next(d for d in range(7) if d in sch.rest_days)
    midday_rest = _utc_for_local(rest_day, 14, acct.proxy.tz_offset)
    assert svc.should_be_active(acct, midday_rest) is False


def test_dm_override_opens_active_window_at_night(svc):
    random.seed(1)
    acct = _Account(tz_offset=0)
    svc.generate_weekly(acct)
    night = _utc_for_local(0, 3, 0)
    assert svc.should_be_active(acct, night) is False
    svc.open_dm_override(acct, now=night)
    assert svc.should_be_active(acct, night) is True            # override active
    later = night + timedelta(seconds=svc.cfg.dm_override_window_s + 60)
    assert svc.should_be_active(acct, later) is False           # override expired


def test_schedule_cache_is_bounded(svc):
    random.seed(2)
    for i in range(svc.MAX_CACHE + 50):
        svc.generate_weekly(_Account(account_id=i))
    assert len(svc._schedules) <= svc.MAX_CACHE
