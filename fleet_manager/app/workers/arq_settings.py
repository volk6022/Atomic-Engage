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
    job_timeout = 300
    keep_result = 3600
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    on_startup = on_startup
    on_shutdown = on_shutdown
    functions = FUNCTIONS
    cron_jobs = [
        cron(deferred_scheduler_tick, second={0, 30}, run_at_startup=False),
        # Daily warmup driver at 03:00 UTC; also runs once on worker startup so a freshly
        # deployed fleet begins warming its accounts immediately (idempotent / deduped).
        cron(warmup_tick, hour={3}, minute={0}, second={0}, run_at_startup=True),
    ]
