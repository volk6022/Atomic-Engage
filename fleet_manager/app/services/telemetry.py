"""Behaviour & ban telemetry recorder (FR-143) — the experiment's primary output.

Writes only the behavioural SHAPE of fleet events (lifecycle transition, cause,
action outcome, the safety params in force) — never message content or PII — so
cohorts run under different safety parameters can be exported and compared (SC-112).

`record` adds the event to the caller's session so it commits ATOMICALLY with the
state transition it describes (no lost or spurious events). It does not commit on its
own; the surrounding `run_task` transaction does. Pass an explicit `cohort` or let it
fall back to the account's denormalised label.
"""
from __future__ import annotations

from typing import Optional

from app.db.models import TelemetryEvent

# Canonical event_type values (data-model D9.3).
ONBOARDED = "onboarded"
WARMUP_TIER = "warmup_tier"
FLOOD = "flood"
SLEEPING = "sleeping"
BANNED = "banned"
ACTION = "action"
SURVIVAL_TICK = "survival_tick"


async def record(
    db,
    *,
    event_type: str,
    account_id: Optional[int] = None,
    cohort: Optional[str] = None,
    cause: Optional[str] = None,
    action_type: Optional[str] = None,
    target_kind: Optional[str] = None,
    outcome: Optional[str] = None,
    warmup_params: Optional[dict] = None,
    flush: bool = False,
) -> TelemetryEvent:
    """Append one telemetry event to `db` (atomic with the caller's transaction)."""
    event = TelemetryEvent(
        account_id=account_id,
        cohort=cohort,
        event_type=event_type,
        cause=cause,
        action_type=action_type,
        target_kind=target_kind,
        outcome=outcome,
        warmup_params=warmup_params,
    )
    db.add(event)
    if flush:
        await db.flush()
    return event


async def record_for_account(db, account, *, event_type: str, **kw) -> TelemetryEvent:
    """Convenience wrapper that pulls `account_id`/`cohort` off an Account row."""
    kw.setdefault("cohort", getattr(account, "cohort", None))
    return await record(db, event_type=event_type, account_id=account.id, **kw)
