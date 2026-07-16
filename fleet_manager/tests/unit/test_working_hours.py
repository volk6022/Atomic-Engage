import pytest
from datetime import datetime

from app.services.working_hours import WorkingHoursGuard


@pytest.fixture
def guard():
    return WorkingHoursGuard()


def test_working_hours_inside_window_allows(guard):
    class MockProxy:
        tz_offset = 10800

    class MockAccount:
        work_start = 9
        work_end = 22
        proxy = MockProxy()

    now_utc = datetime(2026, 5, 4, 12, 0, 0)
    allowed, deferred_until = guard.check(MockAccount(), now_utc)
    assert allowed is True
    assert deferred_until is None


def test_working_hours_outside_window_defers(guard):
    class MockProxy:
        tz_offset = 10800

    class MockAccount:
        work_start = 9
        work_end = 22
        proxy = MockProxy()

    now_utc = datetime(2026, 5, 4, 3, 0, 0)
    allowed, deferred_until = guard.check(MockAccount(), now_utc)
    assert allowed is False
    assert deferred_until is not None


def test_working_hours_tz_offset_applied_correctly(guard):
    class MockProxy:
        tz_offset = 32400

    class MockAccount:
        work_start = 9
        work_end = 22
        proxy = MockProxy()

    now_utc = datetime(2026, 5, 4, 6, 0, 0)
    allowed, deferred_until = guard.check(MockAccount(), now_utc)
    assert allowed is True


def test_working_hours_midnight_boundary(guard):
    class MockProxy:
        tz_offset = 10800

    class MockAccount:
        work_start = 9
        work_end = 22
        proxy = MockProxy()

    now_utc = datetime(2026, 5, 4, 21, 0, 0)
    allowed, deferred_until = guard.check(MockAccount(), now_utc)
    assert allowed is False


def test_working_hours_deferred_until_is_next_window_open(guard):
    class MockProxy:
        tz_offset = 10800

    class MockAccount:
        work_start = 9
        work_end = 22
        proxy = MockProxy()

    now_utc = datetime(2026, 5, 4, 23, 0, 0)
    allowed, deferred_until = guard.check(MockAccount(), now_utc)
    assert allowed is False
    assert deferred_until is not None
