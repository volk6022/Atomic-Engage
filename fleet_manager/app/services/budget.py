"""Profile / premium / aggregate budgets (feature 003, FR-340/341/342).

Two layers:

* **Pure cap math** (``effective_cap`` / ``apply_premium`` / ``decide``) — unit-tested
  directly, no infrastructure.
* **Redis consume** (``check_and_consume``) — enforces the per-account cap *and* the
  per-api_id and per-/24-subnet aggregate budgets, with the **stricter** of the two
  winning. Counters use the Clock-scaled 24 h TTL and the atomic increment so they
  always carry an expiry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.core import safety_config

# Aggregate budget = this fraction of the summed member per-account caps (a
# conservative starting heuristic; hot-reloadable). The mechanism is the point.
AGGREGATE_FRACTION = 0.6

# Actions that carry a fleet-cascade risk and so are also budgeted per api_id / subnet.
SENSITIVE_ACTIONS = frozenset(
    {"messages_per_day", "joins_per_day", "invites_per_day"}
)


def apply_premium(action: str, base_cap: int, is_premium: bool) -> int:
    """Double a cap for premium accounts, clamped to the action's KB ceiling."""
    if not is_premium:
        return base_cap
    ceiling = safety_config.get_premium_ceilings().get(action)
    doubled = base_cap * 2
    return min(doubled, ceiling) if ceiling is not None else doubled


def effective_cap(profile: str, use_case: str, action: str, is_premium: bool) -> int:
    """Per-account daily cap for (profile, use_case, action), premium-adjusted."""
    base = safety_config.rate_limit_for_profile(profile, use_case).get(action, 0)
    return apply_premium(action, base, is_premium)


@dataclass
class BudgetDecision:
    allowed: bool
    binding: Optional[str] = None       # "per_account" | "aggregate" | None
    reason: Optional[str] = None
    retry_after_s: Optional[int] = None


def decide(per_account_remaining: int, aggregate_remaining: Optional[int]) -> BudgetDecision:
    """Stricter-of: an action is allowed only if BOTH the per-account budget and any
    applicable aggregate budget have headroom; the binding constraint is reported."""
    if per_account_remaining <= 0:
        return BudgetDecision(allowed=False, binding="per_account", reason="account_cap")
    if aggregate_remaining is not None and aggregate_remaining <= 0:
        return BudgetDecision(allowed=False, binding="aggregate", reason="aggregate_cap")
    binding = (
        "aggregate"
        if aggregate_remaining is not None and aggregate_remaining < per_account_remaining
        else "per_account"
    )
    return BudgetDecision(allowed=True, binding=binding)


def _aggregate_cap(profile: str, use_case: str, action: str, member_count: int) -> int:
    """Aggregate cap = fraction × (per-account cap × members), but never stricter than
    a single member's own per-account cap. The aggregate exists to stop the *fleet*
    from cascading — it must not throttle a lone account below the individual budget it
    is already entitled to (FR-342). So for one member it equals the per-account cap;
    the 0.6 fraction only bites once ≥2 members share an api_id/subnet."""
    per_account = safety_config.rate_limit_for_profile(profile, use_case).get(action, 0)
    scaled = round(AGGREGATE_FRACTION * per_account * max(1, member_count))
    return max(per_account, scaled)


async def check_and_consume(
    redis,
    clock,
    *,
    account_id: int,
    api_id: int,
    proxy_subnet: str,
    action: str,
    use_case: str,
    cap_profile: str = "conservative",
    is_premium: bool = False,
    api_id_member_count: int = 1,
    subnet_member_count: int = 1,
    ttl_base: int = 86400,
) -> BudgetDecision:
    """Atomically consume one unit of an action's budget if allowed.

    Checks per-account first, then the api_id and /24 aggregate budgets for sensitive
    actions; consumes from every applicable counter only when ALL have headroom.
    """
    from app.db import redis_client as rc

    cap = effective_cap(cap_profile, use_case, action, is_premium)
    if cap <= 0:
        return BudgetDecision(allowed=False, binding="per_account", reason="action_not_allowed")

    # Per-account (peek without committing by computing remaining from current count).
    acct_key = f"budget:acct:{account_id}:{action}"
    acct_count = await rc.rate_limit_peek(redis, acct_key)
    per_account_remaining = cap - acct_count

    aggregate_remaining: Optional[int] = None
    agg_keys: list[tuple[str, int]] = []
    if action in SENSITIVE_ACTIONS:
        api_cap = _aggregate_cap(cap_profile, use_case, action, api_id_member_count)
        subnet_cap = _aggregate_cap(cap_profile, use_case, action, subnet_member_count)
        api_key = f"budget:api:{api_id}:{action}"
        subnet_key = f"budget:net:{proxy_subnet}:{action}"
        api_rem = api_cap - await rc.rate_limit_peek(redis, api_key)
        subnet_rem = subnet_cap - await rc.rate_limit_peek(redis, subnet_key)
        aggregate_remaining = min(api_rem, subnet_rem)
        agg_keys = [(api_key, api_cap), (subnet_key, subnet_cap)]

    decision = decide(per_account_remaining, aggregate_remaining)
    if not decision.allowed:
        return decision

    # Consume from the per-account counter and every applicable aggregate counter.
    # Each increment is atomic (INCR+EXPIRE) and Clock-scaled (FR-350/301).
    await rc.rate_limit_increment(redis, acct_key, ttl=ttl_base, clock=clock)
    for key, _cap in agg_keys:
        await rc.rate_limit_increment(redis, key, ttl=ttl_base, clock=clock)
    return decision
