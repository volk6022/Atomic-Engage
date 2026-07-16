"""Worker task orchestration: account guards, per-account FIFO, and uniform
Telegram error handling (FR-102/104/113).

`run_task` is the single execution path shared by every worker. A worker only supplies
an *action builder* `(payload) -> async (client) -> result`; all lifecycle concerns
(prepare guards, FIFO, executing/complete/failed transitions, FloodWait/ban/PeerIdInvalid
handling, webhooks, enqueue_next) live here so they cannot drift between workers.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.core.clock import get_clock
from app.core.config import get_settings
from app.core.constants import AccountStatus, TaskStatus
from app.core import floodwait as fw
from app.db.models import Account, ApiCredential, Task
from app.services import geo_match, telemetry, working_hours
from app.services.geo_match import RiskLevel, is_datacenter_asn
from app.services.stateless_manager import StatelessManager
from app.services.webhook_sender import WebhookSender
from app.workers import _tg_errors as tg

logger = logging.getLogger(__name__)


def _now() -> datetime:
    # Feature 003: read through the time-scale Clock so flood/working-hours/defer math
    # compresses under TIME_SCALE. At TIME_SCALE=1 this is exactly datetime.now(utc).
    return get_clock().now()


class BaseTask:
    @staticmethod
    async def prepare(ctx, account_id: int, db, account_facing: bool = True) -> Optional[dict]:
        """Account-runnability gate. Returns {"account": account} if the account may
        run a task right now, else None (banned/sleeping/geo-critical/asn-block/flood/
        off-hours).

        `account_facing` is True for behavioural actions (send/join/react/invite) and
        False for warmup-exempt public reads; the datacenter-ASN block-list (FR-310)
        gates only account-facing actions.
        """
        account = (
            await db.execute(
                select(Account).options(selectinload(Account.proxy)).where(
                    Account.id == account_id
                )
            )
        ).scalar_one_or_none()

        if not account:
            return None

        if account.status in (AccountStatus.BANNED, AccountStatus.SLEEPING):
            logger.info("account_%s_not_runnable status=%s", account_id, account.status)
            return None

        geo_result = geo_match.GeoMatchValidator().validate(
            phone_country=account.phone_country,
            proxy_country=account.proxy.country if account.proxy else "XX",
        )
        if geo_result.risk == RiskLevel.CRITICAL:
            account.status = AccountStatus.SLEEPING
            await db.commit()
            await WebhookSender().send(
                delivery_id=0,
                url=get_settings().N8N_SYSTEM_WEBHOOK_URL,
                payload={"event": "geo_reject", "account_id": account_id},
            )
            return None

        # Datacenter-ASN block-list gate (FR-310). Previously declared (GeoMatch rated
        # datacenter ASNs HIGH) but never enforced at dispatch — the audit's worst
        # defect class. Account-facing actions on a datacenter ASN are rejected and the
        # account is put to sleep; warmup-exempt reads are unaffected.
        if (
            account_facing
            and account.proxy is not None
            and is_datacenter_asn(account.proxy.asn)
        ):
            account.status = AccountStatus.SLEEPING
            await db.commit()
            await WebhookSender().send(
                delivery_id=0,
                url=get_settings().N8N_SYSTEM_WEBHOOK_URL,
                payload={
                    "event": "asn_block",
                    "account_id": account_id,
                    "asn": account.proxy.asn,
                },
            )
            return None

        if account.flood_until and account.flood_until > _now():
            return None

        allowed, _ = working_hours.WorkingHoursGuard().check(account, _now())
        if not allowed:
            return None

        return {"account": account}

    @staticmethod
    def compute_defer_until(account) -> Optional[datetime]:
        """When an account is temporarily not runnable, when should the task retry?"""
        if account.flood_until and account.flood_until > _now():
            return account.flood_until
        allowed, defer_until = working_hours.WorkingHoursGuard().check(account, _now())
        if not allowed:
            return defer_until
        return None

    @staticmethod
    async def _other_executing(db, account_id: int, task_id: int) -> bool:
        other = (
            await db.execute(
                select(Task.id).where(
                    Task.account_id == account_id,
                    Task.status == TaskStatus.EXECUTING,
                    Task.id != task_id,
                )
            )
        ).first()
        return other is not None

    @staticmethod
    async def _claim_for_execution(db, task) -> bool:
        """Atomically claim `task` for execution under a per-account advisory lock,
        enforcing strict one-task-at-a-time FIFO (FR-021).

        Two tasks for the SAME account can never both execute: the advisory lock
        serializes every claim attempt for that account, so the "is anything else
        executing? + am I the head of the queue?" check and the EXECUTING write are
        one atomic step. Different accounts use different lock keys and never block
        each other. Returns True if the task is now EXECUTING (committed); False if
        the account is busy or this task is not the queue head (caller leaves it
        QUEUED for enqueue_next).
        """
        account_id = task.account_id
        # Held until the transaction ends (commit/rollback below).
        await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": account_id})

        if await BaseTask._other_executing(db, account_id, task.id):
            await db.rollback()  # release the lock; nothing was written
            return False

        head = (
            await db.execute(
                select(Task.id)
                .where(Task.account_id == account_id, Task.status == TaskStatus.QUEUED)
                .order_by(Task.priority.desc(), Task.created_at, Task.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if head is not None and head != task.id:
            await db.rollback()
            return False

        task.status = TaskStatus.EXECUTING
        task.started_at = _now()
        await db.commit()  # releases the advisory lock
        return True

    @staticmethod
    async def enqueue_next(account_id: int, db, redis) -> None:
        """Enqueue the next queued task for an account, honouring FIFO (one executing
        task at a time). Uses the worker's ARQ redis pool and app.core.config."""
        if redis is None:
            return
        if await BaseTask._other_executing(db, account_id, task_id=-1):
            return
        next_task = (
            await db.execute(
                select(Task)
                .where(Task.account_id == account_id, Task.status == TaskStatus.QUEUED)
                .order_by(Task.priority.desc(), Task.created_at, Task.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if next_task:
            await redis.enqueue_job(next_task.task_type, task_id=next_task.id)


async def _webhook(url: Optional[str], payload: dict, task_id: int = 0) -> None:
    if not url:
        return
    await WebhookSender().send(delivery_id=task_id, url=url, payload=payload)


async def _read_budget_exceeded(redis, account_id: int, read_action: str) -> bool:
    """Consume one unit of `read_action`'s per-account daily budget (§4.1).

    Reads are warmup-exempt but still capped: an over-budget read is deferred, never
    run. Returns True when this call pushed the account over the configured cap.
    Missing redis or an unconfigured action => unlimited (False).
    """
    if redis is None:
        return False
    from app.core import safety_config
    from app.db import redis_client as rc

    cap = safety_config.get_read_limits().get(read_action)
    if not cap:
        return False
    count = await rc.rate_limit_increment(redis, f"read:{read_action}:{account_id}")
    return count > cap


# task_type -> the per-account daily budget action it consumes (feature 003,
# FR-340/342). Reads charge their own budget (see _read_budget_exceeded); warmup_action
# is internal and resolve_username is a warmup-exempt read, so neither is listed here.
WRITE_BUDGET_ACTION = {
    "send_message": "messages_per_day",
    "join_group": "joins_per_day",
    "invite_to_group": "invites_per_day",
    "react": "reactions_per_day",
}

# Behavioural target-kind per task_type for telemetry — shape only, never PII (FR-143).
TARGET_KIND = {
    "send_message": "user",
    "join_group": "group",
    "invite_to_group": "user",
    "react": "message",
    "resolve_username": "peer",
    "get_chat_info": "peer",
    "get_chat_history": "channel",
}


def _warmup_snapshot(account) -> dict:
    """The safety params in force for this account, captured with each event (FR-143)."""
    return {"use_case": account.use_case, "warmup_tier": account.warmup_tier}


async def _humanize_before(task_type: str, payload: dict) -> None:
    """Apply a human pre-action delay before the kurigram call (FR-330-333).

    Replaces the old linear `0.1 s/char` cadence with reading/typing(+typo)/reaction/
    inter-action timing from the Humanizer, all routed through the process Clock so it
    compresses under TIME_SCALE. Gated by HUMANIZE_ACTIONS (off in tests, where the
    60-300 s inter-action floor would otherwise sleep real minutes at scale 1).
    """
    if not get_settings().HUMANIZE_ACTIONS:
        return
    from app.services.humanizer import Humanizer

    h = Humanizer(clock=get_clock())
    if task_type == "send_message":
        await h.typing_delay(str(payload.get("text", "")))
    elif task_type == "react":
        await h.reaction_delay()
    elif task_type in ("join_group", "invite_to_group"):
        await h.inter_action_delay()


async def _consume_write_budget(redis, db, account, task_type: str):
    """Consume one unit of a write-action's daily budget plus its api_id/subnet
    aggregates and return the BudgetDecision (FR-340/341/342).

    Returns None — "no daily-budget gate applies, proceed" — when redis is absent, the
    task_type isn't budgeted, or the action carries no positive cap for the account's
    use_case. A zero cap means the use_case doesn't permit the action at all; that
    permission decision belongs to the warmup/use_case gate, so we skip the daily-limit
    consume here rather than double-gate (and so direct-DB worker tests still run).
    """
    if redis is None:
        return None
    action = WRITE_BUDGET_ACTION.get(task_type)
    if action is None:
        return None
    from app.services import budget

    if budget.effective_cap("conservative", account.use_case, action, False) <= 0:
        return None

    cred = (
        await db.execute(
            select(ApiCredential).where(ApiCredential.id == account.api_credential_id)
        )
    ).scalar_one_or_none()
    api_id = cred.api_id if cred else account.api_credential_id
    members = cred.account_count if cred and cred.account_count else 1

    return await budget.check_and_consume(
        redis,
        get_clock(),
        account_id=account.id,
        api_id=api_id,
        proxy_subnet=f"proxy:{account.proxy_id}",
        action=action,
        use_case=account.use_case,
        cap_profile="conservative",
        is_premium=False,
        api_id_member_count=members,
        subnet_member_count=1,
    )


async def run_task(
    ctx, task_id: int, action_builder, post_process=None, read_action: Optional[str] = None
) -> dict:
    """The single execution path for every worker.

    `action_builder(payload)` returns an async callable `action(client)` that performs
    the kurigram call and returns the success-result dict. Optional
    `post_process(db, redis, account, payload, result)` runs in the same transaction
    after a successful action (e.g. resolve_username persisting a peer). `read_action`,
    when set, charges a warmup-exempt read against that action's per-account daily
    budget and defers the task instead of running it once the cap is hit (§4.1).
    """
    session_maker = ctx["session_maker"]
    redis = ctx.get("redis")
    settings = get_settings()

    async with session_maker() as db:
        task = (
            await db.execute(select(Task).where(Task.id == task_id))
        ).scalar_one_or_none()
        if not task:
            return {"error": "task_not_found"}
        if task.status != TaskStatus.QUEUED:
            return {"error": "task_not_queued", "status": str(task.status)}

        prep = await BaseTask.prepare(
            ctx, task.account_id, db, account_facing=read_action is None
        )
        if prep is None:
            # Re-load account to decide defer vs leave-queued.
            account = (
                await db.execute(select(Account).options(selectinload(Account.proxy)).where(Account.id == task.account_id))
            ).scalar_one_or_none()
            defer_until = BaseTask.compute_defer_until(account) if account else None
            if defer_until:
                task.status = TaskStatus.DEFERRED
                task.deferred_until = defer_until
                await db.commit()
                return {"deferred_until": defer_until.isoformat()}
            # banned / sleeping / geo-critical: leave queued (blocked); do not run.
            return {"blocked": True}

        account = prep["account"]

        # Strict per-account FIFO (FR-021): claim under a per-account advisory lock
        # so two tasks for the SAME account can never both execute, no matter how
        # many workers dispatch concurrently. If not claimed (account busy or this
        # task is not the head of its queue), leave it QUEUED — enqueue_next picks
        # the head when the running task finishes.
        if not await BaseTask._claim_for_execution(db, task):
            return {"queued": True, "reason": "fifo_account_busy"}

        # Read-budget gate (§4.1): warmup-exempt reads still consume a per-account
        # daily budget; over the cap we defer rather than hit Telegram.
        if read_action is not None and await _read_budget_exceeded(
            redis, account.id, read_action
        ):
            until = _now() + timedelta(seconds=3600)
            task.status = TaskStatus.DEFERRED
            task.deferred_until = until
            task.error_code = "READ_BUDGET_EXCEEDED"
            await db.commit()
            await BaseTask.enqueue_next(account.id, db, redis)
            return {"rate_limited": True, "deferred_until": until.isoformat()}

        # Write-action daily-cap gate (FR-340/341/342): behavioural actions with a
        # positive per-day cap consume one unit of the per-account budget plus the
        # api_id and /24-subnet aggregates; the stricter binds. Over cap we defer
        # rather than touch Telegram, then hand the account's queue to the next task.
        if read_action is None:
            decision = await _consume_write_budget(redis, db, account, task.task_type)
            if decision is not None and not decision.allowed:
                until = _now() + timedelta(seconds=3600)
                task.status = TaskStatus.DEFERRED
                task.deferred_until = until
                task.error_code = f"BUDGET_{(decision.binding or 'cap').upper()}"
                await telemetry.record_for_account(
                    db, account,
                    event_type=telemetry.ACTION,
                    action_type=task.task_type,
                    target_kind=TARGET_KIND.get(task.task_type),
                    cause=f"budget_{decision.binding}",
                    outcome="deferred",
                    warmup_params=_warmup_snapshot(account),
                )
                await db.commit()
                await BaseTask.enqueue_next(account.id, db, redis)
                return {
                    "rate_limited": True,
                    "binding": decision.binding,
                    "deferred_until": until.isoformat(),
                }

        # Human pacing (FR-330-333): reading/typing/reaction/inter-action delay before
        # the real Telegram call, through the Clock so it compresses under TIME_SCALE.
        await _humanize_before(task.task_type, task.payload)

        try:
            result = await StatelessManager().execute(
                account.id, action_builder(task.payload), db
            )
            if post_process is not None:
                await post_process(db, redis, account, task.payload, result)
            task.status = TaskStatus.COMPLETE
            task.result = result
            await telemetry.record_for_account(
                db, account,
                event_type=telemetry.ACTION,
                action_type=task.task_type,
                target_kind=TARGET_KIND.get(task.task_type),
                outcome="ok",
                warmup_params=_warmup_snapshot(account),
            )
            await db.commit()
            await _webhook(
                task.webhook_url,
                {"event": "task_complete", "task_id": task.external_id, "result": result},
                task.id,
            )
            return result or {}

        except tg.FloodWait as e:
            base_secs = tg.flood_seconds(e)
            # Adaptive escalation (FR-351): repeated FloodWaits for this account inside
            # the escalation window multiply the wait. The window counter uses the
            # Clock-scaled TTL so it expires correctly under acceleration too.
            prior = 0
            if redis is not None:
                from app.db import redis_client as rc

                count = await rc.rate_limit_increment(
                    redis,
                    f"flood:{account.id}",
                    ttl=fw.ESCALATION_WINDOW_SECONDS,
                    clock=get_clock(),
                )
                prior = max(0, count - 1)
            secs = fw.escalated_wait(base_secs, prior)
            until = _now() + timedelta(seconds=secs)
            account.status = AccountStatus.FLOOD
            account.flood_until = until
            task.status = TaskStatus.DEFERRED
            task.deferred_until = until
            task.error_code = "FLOOD_WAIT"
            await telemetry.record_for_account(
                db, account,
                event_type=telemetry.FLOOD,
                action_type=task.task_type,
                target_kind=TARGET_KIND.get(task.task_type),
                cause=type(e).__name__,
                outcome="flood",
                warmup_params=_warmup_snapshot(account),
            )
            await db.commit()
            await _webhook(
                task.webhook_url or settings.N8N_SYSTEM_WEBHOOK_URL,
                {
                    "event": "flood_wait",
                    "task_id": task.external_id,
                    "account_id": account.id,
                    "flood_until": until.isoformat(),
                },
                task.id,
            )
            return {"flood_until": until.isoformat()}

        except tg.BAN_ERRORS as e:
            account.status = AccountStatus.BANNED
            account.ban_reason = type(e).__name__
            account.flood_until = None
            account.banned_at = _now()        # survival window closes here (FR-143)
            task.status = TaskStatus.FAILED
            task.error_code = "BAN_DETECTED"
            await telemetry.record_for_account(
                db, account,
                event_type=telemetry.BANNED,
                action_type=task.task_type,
                target_kind=TARGET_KIND.get(task.task_type),
                cause=account.ban_reason,
                outcome="banned",
                warmup_params=_warmup_snapshot(account),
            )
            await db.commit()
            await _webhook(
                settings.N8N_SYSTEM_WEBHOOK_URL,
                {
                    "event": "ban_alert",
                    "task_id": task.external_id,
                    "account_id": account.id,
                    "reason": account.ban_reason,
                },
                task.id,
            )
            return {"banned": True, "reason": account.ban_reason}

        except _peer_id_invalid_types() as e:  # noqa: F841
            await _handle_peer_id_invalid(db, redis, task, account.id)
            task.status = TaskStatus.FAILED
            task.error_code = "PEER_ID_INVALID"
            await db.commit()
            await _webhook(
                task.webhook_url,
                {"event": "task_failed", "task_id": task.external_id, "error_code": "PEER_ID_INVALID"},
                task.id,
            )
            return {"error": "peer_id_invalid"}

        except Exception as e:  # noqa: BLE001 — last-resort failure path
            task.status = TaskStatus.FAILED
            task.error_code = type(e).__name__
            await db.commit()
            await _webhook(
                task.webhook_url,
                {"event": "task_failed", "task_id": task.external_id, "error_code": task.error_code},
                task.id,
            )
            return {"error": str(e)}

        finally:
            await BaseTask.enqueue_next(account.id, db, redis)


def _peer_id_invalid_types():
    """Tuple of PeerIdInvalid types to except (empty tuple if unavailable)."""
    return (tg.PeerIdInvalid,) if tg.PeerIdInvalid is not None else tuple()


async def _handle_peer_id_invalid(db, redis, task, account_id: int) -> None:
    """Delete the stale per-account access hash and re-resolve the username (FR-104)."""
    from app.db.models import PeerAccessHash

    payload = task.payload or {}
    peer_id = payload.get("peer_id")
    if peer_id is not None:
        row = (
            await db.execute(
                select(PeerAccessHash).where(
                    PeerAccessHash.account_id == account_id,
                    PeerAccessHash.peer_id == peer_id,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            await db.delete(row)
            await db.flush()

    username = payload.get("recipient_username") or payload.get("username")
    if username and redis is not None:
        try:
            await redis.enqueue_job("resolve_username", task_id=task.id)
        except Exception:  # pragma: no cover - enqueue best-effort
            logger.warning("reresolve_enqueue_failed task=%s", task.id)
