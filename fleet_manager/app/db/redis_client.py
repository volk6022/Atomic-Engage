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


# All-or-nothing check-and-consume across every budget that applies to one action
# (FR-340/341/342). The old shape read each counter with GET, decided in Python, then
# incremented — atomic per increment, but not as a decision: two workers on different
# accounts read the same aggregate value, both saw headroom, and both spent it. The
# overshoot equals the number of concurrent workers, and on 2026-09-05 the fleet's
# join counter stood at 10 against a cap of 9 for exactly that reason.
#
# Two properties the script has to hold and the old shape could not:
#   * nothing is spent unless EVERY counter has headroom — a partial consume would
#     burn an account's daily budget on an action it never performed;
#   * every counter that gets its first increment also gets its expiry, in the same
#     atomic step (the FR-350 rule that made the plain increment a script already).
#
# Returns {0, remaining...} when consumed, or {i} where i is the 1-based index of the
# first counter that refused — the caller maps index 1 to the per-account budget and
# anything beyond it to an aggregate one.
_BUDGET_CONSUME_LUA = """
local n = #KEYS
local ttl = tonumber(ARGV[n + 1])
for i = 1, n do
  local cur = tonumber(redis.call('GET', KEYS[i]) or '0')
  if cur + 1 > tonumber(ARGV[i]) then
    return {i}
  end
end
local out = {0}
for i = 1, n do
  local c = redis.call('INCR', KEYS[i])
  if c == 1 then
    redis.call('EXPIRE', KEYS[i], ttl)
  end
  out[#out + 1] = tonumber(ARGV[i]) - c
end
return out
"""


async def budget_consume(
    redis_client: redis.Redis, budgets: list[tuple[str, int]], ttl: int = 86400,
    clock=None,
) -> tuple[int, list[int]]:
    """Consume one unit from every budget at once, or from none of them.

    `budgets` is ordered `[(key, cap), ...]` with the per-account budget first, so the
    refusal index maps straight back onto the caller's own vocabulary.
    """
    full_keys = [f"rate:{key}" for key, _cap in budgets]
    caps = [str(cap) for _key, cap in budgets]
    effective_ttl = clock.scaled_ttl(ttl) if clock is not None else ttl
    result = await redis_client.eval(
        _BUDGET_CONSUME_LUA, len(full_keys), *full_keys, *caps, str(effective_ttl))
    values = [int(x) for x in result]
    return values[0], values[1:]


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


async def seen_message_setnx(
    redis_client: redis.Redis, chat_id: int, message_id: int, ttl: int = 3600
) -> bool:
    """First-sighting guard for Watcher forwards (Feature 005, US3).

    Atomically records ``seen:{chat_id}:{message_id}`` with a TTL. Returns True when this
    is the FIRST time the message is seen (→ proceed with the webhook), False when it is
    a duplicate (→ suppress). Prevents a Watcher restart from re-forwarding — and thus
    double-triggering — a recently handled post.
    """
    key = f"seen:{chat_id}:{message_id}"
    created = await redis_client.set(key, "1", nx=True, ex=ttl)
    return bool(created)


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
