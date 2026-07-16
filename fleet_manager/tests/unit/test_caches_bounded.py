"""In-memory caches are bounded and evict (003, T074/FR-352).

An unbounded per-account cache is a slow memory leak in a long-running fleet process.
The schedule LRU must cap at MAX_CACHE and drop the least-recently-used entry.
"""
from dataclasses import dataclass

from app.services.schedule_service import ScheduleService


@dataclass
class _Acct:
    id: int
    work_start: int = 8
    work_end: int = 22


def test_schedule_cache_evicts_beyond_max():
    svc = ScheduleService()
    cap = ScheduleService.MAX_CACHE

    for i in range(cap + 50):
        svc.generate_weekly(_Acct(id=i))

    # never grows past the cap …
    assert len(svc._schedules) == cap
    # … and it is the OLDEST ids that were evicted (LRU), newest retained.
    assert 0 not in svc._schedules
    assert (cap + 49) in svc._schedules


def test_schedule_cache_lru_touch_keeps_recently_used():
    svc = ScheduleService()
    cap = ScheduleService.MAX_CACHE

    # Fill exactly to the cap.
    for i in range(cap):
        svc.generate_weekly(_Acct(id=i))
    # Touch id 0 so it is no longer the LRU victim, then overflow by one.
    svc.generate_weekly(_Acct(id=0))
    svc.generate_weekly(_Acct(id=cap))      # forces one eviction

    assert len(svc._schedules) == cap
    assert 0 in svc._schedules              # recently touched → survived
    assert 1 not in svc._schedules          # the new LRU victim
