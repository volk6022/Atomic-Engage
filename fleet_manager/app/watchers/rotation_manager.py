import asyncio
import logging
import random

from app.db.redis_client import (
    watcher_shard_set,
    watcher_shard_list_all,
    get_redis,
)
from sqlalchemy import select

from app.db.models import Account


logger = logging.getLogger(__name__)


class WatcherRotationManager:
    SHARD_SIZE = 75
    ROTATION_MIN = 7200
    ROTATION_MAX = 14400

    def __init__(self, process_id: int):
        self.process_id = process_id
        self.current_shard: list[int] = []
        self.rotation_task: asyncio.Task = None

    async def on_startup(self, db):
        stmt = (
            select(Account)
            .where(Account.status == "active")
            .order_by(Account.last_activity_at.asc())
            .limit(self.SHARD_SIZE)
        )
        result = await db.execute(stmt)
        accounts = result.scalars().all()

        self.current_shard = [a.id for a in accounts]

        redis_conn = await get_redis()
        await watcher_shard_set(redis_conn, self.process_id, self.current_shard)

        logger.info(
            f"shard_claimed process={self.process_id} accounts={len(self.current_shard)}"
        )

        self.rotation_task = asyncio.create_task(self._rotation_loop(db))

        return self.current_shard

    async def _rotation_loop(self, db):
        try:
            while True:
                delay = random.randint(self.ROTATION_MIN, self.ROTATION_MAX)
                await asyncio.sleep(delay)
                await self.rotate(db)
        except asyncio.CancelledError:
            pass

    async def rotate(self, db):
        redis_conn = await get_redis()

        await watcher_shard_set(redis_conn, self.process_id, [])

        stmt = (
            select(Account)
            .where(Account.status == "active")
            .order_by(Account.last_activity_at.asc())
            .limit(self.SHARD_SIZE)
        )
        result = await db.execute(stmt)
        accounts = result.scalars().all()

        self.current_shard = [a.id for a in accounts]

        await watcher_shard_set(redis_conn, self.process_id, self.current_shard)

        logger.info(
            f"shard_rotated process={self.process_id} accounts={len(self.current_shard)}"
        )

    async def crash_recovery(self, db):
        redis_conn = await get_redis()
        shards = await watcher_shard_list_all(redis_conn)

        import psutil

        for pid, account_ids in shards.items():
            if pid == self.process_id:
                continue

            if not psutil.pid_exists(pid):
                logger.info(
                    f"recovering_dead_watcher pid={pid} accounts={len(account_ids)}"
                )

                stmt = select(Account).where(Account.id.in_(account_ids))
                result = await db.execute(stmt)
                recovered_accounts = result.scalars().all()

                self.current_shard = [a.id for a in recovered_accounts]
                await watcher_shard_set(redis_conn, self.process_id, self.current_shard)
                break

    async def heartbeat(self, db):
        redis_conn = await get_redis()
        await watcher_shard_set(redis_conn, self.process_id, self.current_shard)
