"""Weekly schedule, presence, and DM-override (feature 003, FR-320-323).

Ported from soverein ``schedule_service.py`` (values in data-model.md §3): per-account
working hours (09:00-21:00 default, honouring an account's own ``work_start``/
``work_end`` when set), 1 mandatory rest day plus a second at ~30% probability, weekly
regeneration, a 20-minute active override after an inbound DM, and online/offline
presence tied to the window.

**Stateless without a DB (US4 follow-up "заглушка"):** the per-account schedule is a
deterministic function of ``(account.id, virtual week)`` — :meth:`deterministic_schedule`
seeds a local RNG, so every stateless worker derives the *same* schedule for an account
with no persistence and no migration. The in-process bounded cache is only an
optimisation; regeneration anywhere yields an identical result. ``get_schedule_service``
exposes the process-wide stub store that :class:`WorkingHoursGuard` consults.

All time reads go through the injected :class:`Clock`; the cache is bounded (size + a
weekly regeneration boundary) so a long-running 300-session process never grows
unbounded (FR-352).
"""
from __future__ import annotations

import random
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.core.clock import Clock, get_clock
from app.core.humanizer_config import ScheduleConfig

# Anchor for the virtual-week index (any fixed Monday-aligned UTC instant works).
_EPOCH = datetime(2020, 1, 6, tzinfo=timezone.utc)  # a Monday
_WEEK_SECONDS = 7 * 86400


@dataclass
class WeeklySchedule:
    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int
    rest_days: set[int]              # weekday ints (0=Mon .. 6=Sun)
    generated_at: datetime
    week_index: int = 0
    dm_override_until: datetime | None = field(default=None)

    @property
    def start_minutes(self) -> int:
        return self.start_hour * 60 + self.start_minute

    @property
    def end_minutes(self) -> int:
        return self.end_hour * 60 + self.end_minute


def _proxy_tz_offset(account) -> int:
    proxy = getattr(account, "proxy", None)
    return getattr(proxy, "tz_offset", 0) or 0


def _account_window(account):
    """Return (work_start, work_end) if the account pins them, else (None, None)."""
    ws = getattr(account, "work_start", None)
    we = getattr(account, "work_end", None)
    return ws, we


def _is_disabled(ws, we) -> bool:
    """A full-day window (0..24) means 'always on, no rest days' — the convention used
    by test accounts (factory sets work_start=0/work_end=24) to stay time-independent."""
    return ws is not None and we is not None and ws <= 0 and we >= 24


class ScheduleService:
    MAX_CACHE = 500  # bounded LRU of per-account schedules (FR-352)

    def __init__(self, cfg: ScheduleConfig | None = None, clock: Clock | None = None):
        self.cfg = cfg or ScheduleConfig()
        self.clock = clock or Clock()
        self._schedules: "OrderedDict[int, WeeklySchedule]" = OrderedDict()

    # ---- week index --------------------------------------------------------------
    def _week_index(self, now: datetime) -> int:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return int((now - _EPOCH).total_seconds() // _WEEK_SECONDS)

    # ---- generation --------------------------------------------------------------
    def _generate(self, rng, account, now: datetime) -> WeeklySchedule:
        """Build a schedule using ``rng`` (the global ``random`` module for the
        non-deterministic API, or a seeded ``random.Random`` for the stateless store)."""
        cfg = self.cfg
        ws, we = _account_window(account)

        if _is_disabled(ws, we):
            return WeeklySchedule(
                start_hour=0, start_minute=0, end_hour=24, end_minute=0,
                rest_days=set(), generated_at=now, week_index=self._week_index(now),
            )

        start_hour = ws if ws is not None else rng.randint(cfg.work_start_min_h, cfg.work_start_max_h)
        end_hour = we if we is not None else rng.randint(cfg.work_end_min_h, cfg.work_end_max_h)
        start_minute = 0 if ws is not None else rng.choice(cfg.minute_granularity)
        end_minute = 0 if we is not None else rng.choice(cfg.minute_granularity)

        rest_days = {rng.randint(0, 6)}
        if rng.random() < cfg.second_rest_day_prob:
            candidates = [d for d in range(7) if d not in rest_days]
            rest_days.add(rng.choice(candidates))

        return WeeklySchedule(
            start_hour=start_hour, start_minute=start_minute,
            end_hour=end_hour, end_minute=end_minute,
            rest_days=rest_days, generated_at=now, week_index=self._week_index(now),
        )

    def generate_weekly(self, account) -> WeeklySchedule:
        """Random (non-deterministic) generation — the standalone API."""
        sched = self._generate(random, account, self.clock.now())
        self._store(account, sched)
        return sched

    def deterministic_schedule(self, account, now: datetime | None = None) -> WeeklySchedule:
        """Stateless, reproducible schedule seeded by (account.id, virtual week).

        Any worker, any process, any restart derives the identical schedule for an
        account within a given week — no DB needed.
        """
        now = now or self.clock.now()
        week = self._week_index(now)
        seed = (int(getattr(account, "id", 0)) * 1_000_003 + week) & 0x7FFFFFFF
        sched = self._generate(random.Random(seed), account, now)
        self._store(account, sched)
        return sched

    def _store(self, account, sched: WeeklySchedule) -> None:
        key = getattr(account, "id", None)
        if key is None:
            return
        self._schedules[key] = sched
        self._schedules.move_to_end(key)
        while len(self._schedules) > self.MAX_CACHE:
            self._schedules.popitem(last=False)  # evict oldest (LRU)

    def _get(self, account) -> WeeklySchedule:
        """Cached schedule for the current virtual week, regenerating on a new week or
        when absent. Deterministic for id-bearing accounts; random for id-less mocks."""
        now = self.clock.now()
        week = self._week_index(now)
        key = getattr(account, "id", None)
        sched = self._schedules.get(key) if key is not None else None
        if sched is not None and sched.week_index == week:
            self._schedules.move_to_end(key)
            return sched
        if key is not None:
            preserved = sched.dm_override_until if sched is not None else None
            sched = self.deterministic_schedule(account, now)
            sched.dm_override_until = preserved
            return sched
        return self.generate_weekly(account)

    # ---- queries -----------------------------------------------------------------
    def _local(self, account, now: datetime | None) -> datetime:
        now = now or self.clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now + timedelta(seconds=_proxy_tz_offset(account))

    def should_be_active(self, account, now: datetime | None = None) -> bool:
        now = now or self.clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        sched = self._get(account)
        if sched.dm_override_until is not None and now < sched.dm_override_until:
            return True
        local = self._local(account, now)
        if local.weekday() in sched.rest_days:
            return False
        cur = local.hour * 60 + local.minute
        return sched.start_minutes <= cur < sched.end_minutes

    def next_open(self, account, now: datetime | None = None) -> datetime | None:
        """Next instant (virtual UTC) the account becomes active, or None if active."""
        now = now or self.clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if self.should_be_active(account, now):
            return None
        sched = self._get(account)
        if not sched.rest_days and sched.start_minutes == 0 and sched.end_minutes >= 24 * 60:
            return None  # disabled schedule is never closed
        local_now = self._local(account, now)
        for day_ahead in range(0, 8):
            probe = local_now + timedelta(days=day_ahead)
            if probe.weekday() in sched.rest_days:
                continue
            open_local = probe.replace(
                hour=sched.start_hour, minute=sched.start_minute, second=0, microsecond=0
            )
            open_utc = open_local - timedelta(seconds=_proxy_tz_offset(account))
            if open_utc > now:
                return open_utc
        return now + timedelta(days=1)

    def open_dm_override(self, account, now: datetime | None = None) -> None:
        now = now or self.clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        sched = self._get(account)
        sched.dm_override_until = now + timedelta(seconds=self.cfg.dm_override_window_s)
        self._store(account, sched)

    def presence(self, account, now: datetime | None = None) -> str:
        return "online" if self.should_be_active(account, now) else "offline"


# --------------------------------------------------------------------------------------
# Process-wide stub store (the "заглушка" replacing a DB schedule table).
# --------------------------------------------------------------------------------------
_schedule_service: ScheduleService | None = None


def get_schedule_service() -> ScheduleService:
    """The shared, deterministic schedule store consulted by WorkingHoursGuard.

    Bound to the process Clock so it compresses under TIME_SCALE. Because schedules are
    derived from (account.id, week), this in-process singleton is consistent with any
    other worker that derives the same account's schedule — no shared DB required.
    """
    global _schedule_service
    if _schedule_service is None:
        _schedule_service = ScheduleService(ScheduleConfig(), get_clock())
    return _schedule_service
