import json
from typing import Optional

import redis.asyncio as redis

from app.core.config import get_settings


_redis_client: Optional[redis.Redis] = None


async def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


async def peer_cache_get(redis_client: redis.Redis, username: str) -> Optional[int]:
    key = f"u2p:{username}"
    peer_id_str = await redis_client.get(key)
    if peer_id_str:
        return int(peer_id_str)
    return None


async def peer_cache_set(
    redis_client: redis.Redis, username: str, peer_id: int, ttl: int = 86400
) -> None:
    key = f"u2p:{username}"
    await redis_client.setex(key, ttl, str(peer_id))


# Atomic INCR + first-write EXPIRE in one round-trip (feature 003, FR-350). The old
# two-step (INCR then conditional EXPIRE) had a crash window: a process death between
# the two left an eternal, TTL-less counter that wedged an account over-cap forever,
# and it raced under concurrent workers. The Lua script makes both one atomic step.
_RATE_LIMIT_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return c
"""


async def rate_limit_increment(
    redis_client: redis.Redis, key: str, ttl: int = 86400, clock=None
) -> int:
    """Atomically increment ``rate:{key}`` and set its TTL on first write.

    A counter can NEVER exist without an expiry. When a ``clock`` is supplied the TTL
    is compressed by ``TIME_SCALE`` so a 24 h virtual budget resets in ~30 real min at
    48× (FR-301).
    """
    full_key = f"rate:{key}"
    effective_ttl = clock.scaled_ttl(ttl) if clock is not None else ttl
    return int(await redis_client.eval(_RATE_LIMIT_LUA, 1, full_key, effective_ttl))


async def rate_limit_peek(redis_client: redis.Redis, key: str) -> int:
    """Current value of ``rate:{key}`` without mutating it (0 if unset)."""
    raw = await redis_client.get(f"rate:{key}")
    return int(raw) if raw else 0


async def chat_info_cache_get(
    redis_client: redis.Redis, username: str
) -> Optional[dict]:
    """Cached `get_chat_info` result (§4.5; bursty, re-runnable enrichment)."""
    key = f"chatinfo:{username.lower()}"
    raw = await redis_client.get(key)
    if raw:
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None
    return None


async def chat_info_cache_set(
    redis_client: redis.Redis, username: str, info: dict, ttl: int = 604800
) -> None:
    key = f"chatinfo:{username.lower()}"
    await redis_client.setex(key, ttl, json.dumps(info, default=str))


async def proxy_health_set(
    redis_client: redis.Redis, proxy_id: int, is_healthy: bool, ttl: int = 3600
) -> None:
    key = f"proxy:health:{proxy_id}"
    await redis_client.setex(key, ttl, "1" if is_healthy else "0")


async def proxy_health_get(redis_client: redis.Redis, proxy_id: int) -> bool:
    key = f"proxy:health:{proxy_id}"
    value = await redis_client.get(key)
    return value == "1"


async def watcher_shard_set(
    redis_client: redis.Redis, process_id: int, account_ids: list[int]
) -> None:
    key = f"watcher:shard:{process_id}"
    await redis_client.setex(key, 7200, json.dumps(account_ids))


async def watcher_shard_get(
    redis_client: redis.Redis, process_id: int
) -> Optional[list[int]]:
    key = f"watcher:shard:{process_id}"
    value = await redis_client.get(key)
    if value:
        return json.loads(value)
    return None


async def watcher_shard_list_all(redis_client: redis.Redis) -> dict[int, list[int]]:
    keys = []
    async for key in redis_client.scan_iter(match="watcher:shard:*"):
        keys.append(key)
    result = {}
    for key in keys:
        process_id = int(key.split(":")[-1])
        value = await redis_client.get(key)
        if value:
            result[process_id] = json.loads(value)
    return result
