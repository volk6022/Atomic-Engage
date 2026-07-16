"""Per-account activity gate used by the worker prepare path.

For real (id-bearing) accounts this delegates to the deterministic schedule store
(:mod:`app.services.schedule_service`), so the worker now honours the full weekly
schedule — nightly window, 1-2 rest days, DM-override, presence — derived statelessly
from ``(account.id, virtual week)`` with no DB (feature 003, US4).

Id-less mock accounts (as used by ``test_working_hours``) keep the original pure
flat-window behaviour, so nothing that drove the gate by ``work_start``/``work_end``
directly changes.
"""
from datetime import datetime, timedelta
from typing import Optional


class WorkingHoursGuard:
    def check(self, account, now_utc: datetime) -> tuple[bool, Optional[datetime]]:
        if getattr(account, "id", None) is not None:
            return self._schedule_check(account, now_utc)
        return self._flat_check(account, now_utc)

    def _schedule_check(
        self, account, now_utc: datetime
    ) -> tuple[bool, Optional[datetime]]:
        from app.services.schedule_service import get_schedule_service

        svc = get_schedule_service()
        if svc.should_be_active(account, now_utc):
            return True, None
        return False, svc.next_open(account, now_utc)

    def _flat_check(
        self, account, now_utc: datetime
    ) -> tuple[bool, Optional[datetime]]:
        proxy_tz_offset = account.proxy.tz_offset if hasattr(account, "proxy") else 0
        local_hour = (
            now_utc.hour * 3600 + now_utc.minute * 60 + now_utc.second + proxy_tz_offset
        ) // 3600
        local_hour = local_hour % 24

        if account.work_start <= local_hour < account.work_end:
            return True, None

        next_day = now_utc.replace(hour=0, minute=0, second=0)
        next_day = next_day.replace(hour=account.work_start)

        if local_hour >= account.work_end:
            next_day = next_day + timedelta(days=1)

        return False, next_day
