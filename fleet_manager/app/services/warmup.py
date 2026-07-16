from datetime import datetime, timedelta
from typing import Optional

from app.core.constants import WarmupTier
from app.core import safety_config
# Re-exported so the FR-110 / data-model §9 test can assert on the canonical
# defaults; the LIVE values are read through safety_config (hot-reloadable, FR-145).
from app.core.safety_defaults import TIER_ORDER, WARMUP_SCHEDULES  # noqa: F401


def _tiers(use_case: str) -> dict:
    """Tiers for a use-case from the HOT-reloadable safety config (FR-145)."""
    sched = safety_config.get_warmup_schedules().get(use_case)
    return sched["tiers"] if sched else {}


def _cumulative_days(use_case: str, through_tier: str) -> int:
    """Warmup-days required to COMPLETE `through_tier` (sum of every tier's `days`
    from fresh up to and including it)."""
    tiers = _tiers(use_case)
    total = 0
    for t in TIER_ORDER:
        cfg = tiers.get(t)
        if cfg:
            total += cfg["days"]
        if t == through_tier:
            break
    return total


class WarmupPipeline:
    def get_allowed_actions(self, account) -> list[str]:
        cfg = _tiers(account.use_case).get(account.warmup_tier)
        return list(cfg["actions"]) if cfg else []

    async def advance_tier_if_due(self, account, db, webhook_sender) -> Optional[str]:
        if account.warmup_day < 0 or account.warmup_tier == WarmupTier.READY:
            return None

        use_case = account.use_case
        if use_case not in WARMUP_SCHEDULES:
            return None

        current_tier = account.warmup_tier
        if current_tier not in TIER_ORDER:
            return None

        current_idx = TIER_ORDER.index(current_tier)
        if current_idx + 1 >= len(TIER_ORDER):
            return None

        # Advance only after the account has spent the full cumulative day budget
        # for the current tier (per-tier `days` summed fresh..current) — FR-110.
        if account.warmup_day < _cumulative_days(use_case, current_tier):
            return None

        new_tier = TIER_ORDER[current_idx + 1]

        if new_tier != account.warmup_tier:
            from app.core.constants import AccountStatus
            from app.core.config import get_settings
            from app.services.webhook_sender import WebhookSender

            old_tier = account.warmup_tier
            account.warmup_tier = new_tier

            if new_tier == WarmupTier.READY:
                account.status = AccountStatus.ACTIVE

            from app.services import telemetry

            await telemetry.record_for_account(
                db, account,
                event_type=telemetry.WARMUP_TIER,
                cause=f"{old_tier}->{new_tier}",
                outcome="ready" if new_tier == WarmupTier.READY else "ok",
                warmup_params={"use_case": account.use_case, "warmup_tier": new_tier},
            )
            await db.commit()

            settings = get_settings()
            webhook = WebhookSender()

            event = {
                "event": "warmup_transition"
                if new_tier != WarmupTier.READY
                else "warmup_complete",
                "account_id": account.id,
                "from_tier": old_tier,
                "to_tier": new_tier,
            }

            await webhook.send(
                delivery_id=0, url=settings.N8N_SYSTEM_WEBHOOK_URL, payload=event
            )

            return new_tier

        return None

    async def select_cross_pair(
        self, source_account_id: int, db, target_pool: list[int]
    ) -> Optional[int]:
        from sqlalchemy import select
        from app.db.models import WarmupCrossPair
        from app.core.clock import get_clock

        # Enforce the 48h per-pair cooldown (FR-311) by the Clock-written cooldown_until
        # (virtual time), NOT a wall-clock created_at window — so the cooldown actually
        # compresses under TIME_SCALE in the accelerated harness. A pair is "on cooldown"
        # while its cooldown_until is still in the (virtual) future.
        now = get_clock().now()
        stmt = select(WarmupCrossPair).where(
            WarmupCrossPair.source_account_id == source_account_id,
            WarmupCrossPair.cooldown_until > now,
        )
        result = await db.execute(stmt)
        pairs = result.scalars().all()

        used_targets = {p.target_account_id for p in pairs}

        available = [
            t for t in target_pool if t not in used_targets and t != source_account_id
        ]

        if not available:
            return None

        for candidate in available:
            if await self._would_form_ring(source_account_id, candidate, db):
                continue
            return candidate

        return None

    async def _would_form_ring(self, source: int, target: int, db) -> bool:
        from sqlalchemy import select
        from app.db.models import WarmupCrossPair
        from collections import deque

        visited: set[int] = set()
        queue: deque[int] = deque([target])

        while queue:
            current = queue.popleft()
            if current == source:
                return True
            if current in visited:
                continue
            visited.add(current)

            stmt = select(WarmupCrossPair.target_account_id).where(
                WarmupCrossPair.source_account_id == current
            )
            result = await db.execute(stmt)
            for (neighbor,) in result.all():
                if neighbor not in visited:
                    queue.append(neighbor)

        return False

    async def enqueue_daily_warmup_action(self, account, db, redis) -> bool:
        """Enqueue exactly one warmup action for `account` for its current warmup_day.

        Deduped via a Redis key keyed by (account, warmup_day) so repeated ticks on the
        same day are idempotent. Returns True when an action was newly enqueued. The
        action picked is the first of the current tier's allowed actions (data-model §9);
        the worker performs a lightweight, non-outbound client touch.
        """
        import uuid

        from app.core.config import get_settings
        from app.core.constants import AccountStatus, TaskStatus, WarmupTier
        from app.db.models import Task

        if redis is None:
            return False
        if account.status != AccountStatus.WARMUP or account.warmup_tier == WarmupTier.READY:
            return False

        key = f"warmup:done:{account.id}:{account.warmup_day}"
        if await redis.get(key):
            return False

        allowed = self.get_allowed_actions(account)
        action_name = allowed[0] if allowed else "profile_setup"
        task = Task(
            external_id=str(uuid.uuid4()),
            account_id=account.id,
            task_type="warmup_action",
            payload={"action": action_name},
            status=TaskStatus.QUEUED,
            webhook_url=get_settings().N8N_SYSTEM_WEBHOOK_URL,
            priority=3,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        await redis.enqueue_job("warmup_action", task_id=task.id)
        await redis.setex(key, 172_800, "1")  # 2-day TTL; comfortably covers one warmup day
        return True

    async def schedule_cross_message(self, source_id: int, target_id: int, db) -> None:
        from app.db.models import WarmupCrossPair
        from app.core.clock import get_clock

        # 48h pair cooldown stamped in virtual time (FR-311) so it compresses under
        # TIME_SCALE and is honoured by select_cross_pair's cooldown_until filter.
        cooldown = get_clock().now() + timedelta(days=2)

        pair = WarmupCrossPair(
            source_account_id=source_id,
            target_account_id=target_id,
            action_type="cross_message_reply",
            cooldown_until=cooldown,
        )
        db.add(pair)
        await db.commit()

        from app.db.models import Task

        task = Task(
            external_id=f"warmup-{source_id}-{target_id}",
            account_id=source_id,
            task_type="warmup_action",
            payload={"action": "cross_message_reply", "target_id": target_id},
            status="queued",
        )
        db.add(task)
        await db.commit()


async def run_warmup_tick(session_maker, redis, now=None) -> dict:
    """Drive every warming account forward by one tick (Phase 7 / US6).

    This is the loop that actually RUNS warmup (wired as the ARQ `warmup_tick` cron).
    For each `warmup` account it: (1) advances `warmup_day` to the real elapsed days
    since warmup started (created_at — an account is created at onboarding in `warmup`),
    (2) promotes the tier when the cumulative day budget is met (firing the
    warmup_transition/complete webhook, flipping to `active` at `ready`), and (3)
    enqueues exactly one warmup action per account per day (deduped in Redis).

    Idempotent: safe to run on every cron firing and on worker restart.
    """
    from datetime import timezone

    from sqlalchemy import select

    from app.core.clock import get_clock
    from app.core.constants import AccountStatus
    from app.db.models import Account
    from app.services.webhook_sender import WebhookSender

    # Behavioural time through the Clock (R2/FR-302): warmup-day advancement compresses
    # under TIME_SCALE so a virtual week of warmup runs in the accelerated cycle. A
    # no-op vs datetime.now(utc) at TIME_SCALE=1.
    now = now or get_clock().now()
    result: dict = {"advanced": [], "enqueued": []}
    pipeline = WarmupPipeline()
    sender = WebhookSender()

    async with session_maker() as db:
        accounts = (
            await db.execute(
                select(Account).where(Account.status == AccountStatus.WARMUP)
            )
        ).scalars().all()

        for account in accounts:
            started = account.created_at
            if started is not None:
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                elapsed = (now - started).days
                if elapsed > account.warmup_day:
                    account.warmup_day = elapsed
                    await db.commit()

            new_tier = await pipeline.advance_tier_if_due(account, db, sender)
            if new_tier:
                result["advanced"].append((account.id, new_tier))

            if await pipeline.enqueue_daily_warmup_action(account, db, redis):
                result["enqueued"].append(account.id)

    return result
