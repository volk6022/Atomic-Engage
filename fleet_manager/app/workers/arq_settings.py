import logging

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.db.session import get_session_maker
from app.workers.send_message import send_message
from app.workers.join_group import join_group
from app.workers.react import react
from app.workers.resolve_username import resolve_username
from app.workers.invite_to_group import invite_to_group
from app.workers.warmup_action import warmup_action
from app.workers.get_chat_info import get_chat_info
from app.workers.get_chat_history import get_chat_history
from app.workers.get_chat_admins import get_chat_admins
from app.workers.get_dialogs import get_dialogs
from app.workers.get_similar_channels import get_similar_channels
from app.workers.search_public_chats import search_public_chats
from app.workers.deliver_webhook import deliver_webhook
from app.workers.recovery import recover_orphaned_tasks, reenqueue_due_deferred

logger = logging.getLogger(__name__)

FUNCTIONS = [
    send_message,
    join_group,
    react,
    resolve_username,
    invite_to_group,
    warmup_action,
    get_chat_info,
    get_chat_history,
    get_chat_admins,
    get_dialogs,
    get_similar_channels,
    search_public_chats,
    deliver_webhook,
]

settings = get_settings()


async def on_startup(ctx):
    """Provide a DB session factory to every job and run crash recovery.

    NOTE: arq already places its ArqRedis pool in ctx['redis']; we must ADD to ctx,
    not replace it (the original bug returned a dict that arq ignored, leaving workers
    with no DB session — defect C1).
    """
    ctx["session_maker"] = get_session_maker()
    session_maker = ctx["session_maker"]
    async with session_maker() as db:
        await recover_orphaned_tasks(db, redis=ctx.get("redis"))


async def on_shutdown(ctx):
    pass


async def deferred_scheduler_tick(ctx):
    """Cron job (every 30s): re-enqueue deferred tasks whose window has opened."""
    session_maker = ctx["session_maker"]
    async with session_maker() as db:
        await reenqueue_due_deferred(db, redis=ctx.get("redis"))


async def recover_orphaned_tick(ctx):
    """Cron job (every 5 min): re-queue tasks stuck in `executing` past the recovery lease.

    on_startup only covers worker boots; a job killed by job_timeout mid-humanizer-pause
    would otherwise keep its account's FIFO queue frozen until the next deploy/restart
    (prod, 04.09.2026 — recovery exists for exactly this, but was startup-only).
    """
    session_maker = ctx["session_maker"]
    async with session_maker() as db:
        recovered = await recover_orphaned_tasks(db, redis=ctx.get("redis"))
    if recovered:
        logger.info("recover_orphaned_tick recovered=%s ids=%s", len(recovered), recovered)


async def warmup_tick(ctx):
    """Cron job (daily): drive warming accounts forward — advance warmup_day, promote
    tiers when due, and enqueue one warmup action per account per day (US6)."""
    from app.services.warmup import run_warmup_tick

    summary = await run_warmup_tick(ctx["session_maker"], ctx.get("redis"))
    if summary["advanced"] or summary["enqueued"]:
        logger.info(
            "warmup_tick advanced=%s enqueued=%s",
            summary["advanced"],
            summary["enqueued"],
        )


class WorkerSettings:
    max_jobs = 10
    # Derived from HumanizerConfig (app/core/humanizer_config.py): join_group and
    # invite_to_group sleep inter_action_base_s * (1 + inter_action_jitter) =
    # 300 * 1.40 = 420 s at most before touching Telegram (base_task._humanize_before),
    # and the contract (tests/unit/test_job_timeout_vs_humanizer.py) requires >= 60 s
    # on top of that for the proxy connect + the call itself — a hard floor of 480 s.
    # We take 600 s instead of the bare floor because the 60 s margin is a *minimum*
    # and mobile proxies routinely eat tens of extra seconds; a job killed mid-pause
    # is closed by no one and freezes the account's FIFO queue (prod, 04.09.2026).
    job_timeout = 600
    keep_result = 3600
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    on_startup = on_startup
    on_shutdown = on_shutdown
    functions = FUNCTIONS
    cron_jobs = [
        cron(deferred_scheduler_tick, second={0, 30}, run_at_startup=False),
        # Orphan sweep every 5 min: the recovery lease (900 s) already bounds how long a
        # dead task can hold an account's queue, and this tick only trims the tail after
        # it expires — a sub-minute cadence would add DB load for at most minutes of
        # gain, while a much sparser one would leave freshly-orphaned accounts parked
        # for no reason. run_at_startup=False because on_startup already sweeps on boot.
        cron(recover_orphaned_tick, minute=set(range(0, 60, 5)), second=0, run_at_startup=False),
        # Daily warmup driver at 03:00 UTC; also runs once on worker startup so a freshly
        # deployed fleet begins warming its accounts immediately (idempotent / deduped).
        cron(warmup_tick, hour={3}, minute={0}, second={0}, run_at_startup=True),
    ]
