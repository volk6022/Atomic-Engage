from sqlalchemy import select
from typing import Optional

import redis.asyncio as redis

from app.db import redis_client as redis_helpers


class PeerResolver:
    def __init__(self):
        self.daily_resolve_limit = 50

    async def resolve(
        self, username: str, db, redis_conn: redis.Redis
    ) -> Optional[int]:
        cached_peer_id = await redis_helpers.peer_cache_get(redis_conn, username)
        if cached_peer_id:
            return cached_peer_id

        return await self.select_account_for_resolution([1, 2, 3], redis_conn)

    async def select_account_for_resolution(
        self, account_ids: list[int], redis_conn: redis.Redis
    ) -> Optional[int]:
        for account_id in account_ids:
            if await self._check_rate_limit(redis_conn, account_id):
                return account_id
        return None

    async def _check_rate_limit(self, redis_conn: redis.Redis, account_id: int) -> bool:
        count = await redis_helpers.rate_limit_increment(
            redis_conn, f"resolve:{account_id}", ttl=86400
        )
        return count <= self.daily_resolve_limit

    async def upsert_peer(
        self, db, peer_id: int, username: str, access_hash: int, account_id: int
    ) -> None:
        """Upsert the global username->peer_id mapping and the per-account access hash.

        The access hash is keyed by (account_id, peer_id) — never shared across
        accounts (Principle IV / FR-109/FR-010).
        """
        from app.db.models import GlobalPeer, PeerAccessHash

        stmt = select(GlobalPeer).where(GlobalPeer.peer_id == peer_id)
        result = await db.execute(stmt)
        global_peer = result.scalar_one_or_none()

        if not global_peer:
            global_peer = GlobalPeer(peer_id=peer_id, username=username)
            db.add(global_peer)
            await db.flush()

        stmt = select(PeerAccessHash).where(
            PeerAccessHash.account_id == account_id,
            PeerAccessHash.peer_id == peer_id,
        )
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()

        if not existing:
            access_hash_row = PeerAccessHash(
                account_id=account_id,
                peer_id=peer_id,
                access_hash=access_hash,
            )
            db.add(access_hash_row)
            await db.flush()
