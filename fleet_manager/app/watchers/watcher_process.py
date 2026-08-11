import asyncio
import logging
import os
import sys
from pyrogram import Client, filters

from app.db.session import get_engine, get_session_maker
from app.db.redis_client import get_redis
from app.db.models import Base
from app.watchers.rotation_manager import WatcherRotationManager
from app.watchers.update_handler import UpdateHandler
from app.core import logging as app_logging


logger = logging.getLogger(__name__)

# A watcher restarts itself (Docker brings it back, reclaiming its shard) when host
# memory crosses this line. Module-level + pure so it is unit-testable, unlike the old
# inline closure (US2; SC).
MEMORY_RESTART_THRESHOLD = 85.0

# How long a watcher whose shard is empty waits before looking again. Long enough that
# an idle process costs nothing, short enough that a newly onboarded account starts
# being watched within a minute.
EMPTY_SHARD_RECHECK_SECONDS = 60


def memory_over_threshold(threshold: float = MEMORY_RESTART_THRESHOLD) -> bool:
    """True when host memory usage exceeds `threshold` percent."""
    import psutil

    return psutil.virtual_memory().percent > threshold


async def main():
    app_logging.setup_logging()

    # Defaults to a single process owning the whole fleet. The old fallback was
    # os.getpid(), which inside a container is always 1 -- so four watcher containers
    # all identified as process 1 and, with the unsharded query, each claimed every
    # account. Set WATCHER_PROCESS_ID (1..N) and WATCHER_TOTAL=N per container.
    process_id = int(os.environ.get("WATCHER_PROCESS_ID", 1))
    total_processes = int(os.environ.get("WATCHER_TOTAL", 1))
    logger.info(
        f"watcher_starting process_id={process_id}/{total_processes}"
    )

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = get_session_maker()
    redis_conn = await get_redis()

    rotation_mgr = WatcherRotationManager(process_id, total_processes)

    # An empty shard is normal, not a reason to die. With fewer active accounts than
    # watcher processes some shards are simply empty, and exiting turned
    # `restart: unless-stopped` into a crash loop -- observed on two production
    # instances that have one account against four watchers. Wait and re-check
    # instead: an account onboarded later lands in a shard whose process is already
    # up, and picks it up on the next sweep without anyone restarting anything.
    async with session_maker() as db:
        account_ids = await rotation_mgr.on_startup(db)

    while not account_ids:
        logger.info(
            "no_accounts_in_shard process=%s/%s; re-checking in %ss",
            process_id, total_processes, EMPTY_SHARD_RECHECK_SECONDS,
        )
        await asyncio.sleep(EMPTY_SHARD_RECHECK_SECONDS)
        async with session_maker() as db:
            account_ids = await rotation_mgr.on_startup(db)

    clients = []

    async with session_maker() as db:
        from sqlalchemy import select
        from app.db.models import Account, ApiCredential, Proxy

        for account_id in account_ids:
            stmt = select(Account).where(Account.id == account_id)
            result = await db.execute(stmt)
            account = result.scalar_one_or_none()

            if not account:
                continue

            stmt = select(ApiCredential).where(
                ApiCredential.id == account.api_credential_id
            )
            result = await db.execute(stmt)
            credential = result.scalar_one_or_none()

            stmt = select(Proxy).where(Proxy.id == account.proxy_id)
            result = await db.execute(stmt)
            proxy = result.scalar_one_or_none()

            try:
                client = Client(
                    name=f"watch_{account_id}",
                    in_memory=True,
                    session_string=account.session_string,
                    api_id=credential.api_id,
                    api_hash=credential.api_hash,
                    proxy=proxy.url if proxy else None,
                    device_model=account.device_model,
                    system_version=account.system_version,
                    app_version=account.app_version,
                    lang_code=account.lang_code,
                    system_lang_code=account.system_lang_code,
                )

                # bind account_id per-iteration (default arg) so every handler
                # attributes messages to its OWN account, not the loop's last (H4).
                @client.on_message(filters.incoming)
                async def handle_msg(client, message, account_id=account_id):
                    # Pass the session_maker (not a live session): the handler opens a
                    # fresh per-message session so concurrent updates never share one
                    # (Constitution I; Feature 005 contract).
                    await UpdateHandler().handle_new_message(
                        client=client,
                        account_id=account_id,
                        message=message,
                        db=session_maker,
                        redis_conn=redis_conn,
                    )

                clients.append(client)
                logger.info(f"client_created account_id={account_id}")

            except Exception as e:
                logger.error(f"client_creation_failed account={account_id}: {e}")

    logger.info(f"watcher_ready process_id={process_id} clients={len(clients)}")

    async def monitor_memory():
        while True:
            await asyncio.sleep(30)
            if memory_over_threshold():
                logger.warning("memory_threshold_exceeded")
                sys.exit(1)

    memory_task = asyncio.create_task(monitor_memory())

    async def heartbeat_loop():
        while True:
            await asyncio.sleep(60)
            async with session_maker() as db:
                await rotation_mgr.heartbeat(db)

    heartbeat_task = asyncio.create_task(heartbeat_loop())

    # Start every monitored client (each connects through its proxy) and keep the
    # process alive so the incoming-message handlers fire. A single bad proxy must
    # not abort the whole shard, so each start() is guarded independently.
    started = []
    for c in clients:
        try:
            await c.start()
            started.append(c)
        except Exception as e:  # noqa: BLE001
            logger.error(f"client_start_failed: {e}")

    logger.info(f"watcher_running process_id={process_id} started={len(started)}")
    try:
        await asyncio.Event().wait()  # run until the process is cancelled/killed
    finally:
        for c in started:
            try:
                await c.stop()
            except Exception:  # noqa: BLE001
                pass
        memory_task.cancel()
        heartbeat_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
