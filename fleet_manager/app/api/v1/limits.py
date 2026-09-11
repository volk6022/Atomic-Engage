"""Read-only exposure of the fleet's daily budgets: ``GET /v1/limits`` (E1).

A peek, never a spend: counters are read with peek-semantics
(:func:`app.db.redis_client.rate_limit_peek_many` — one GET+TTL pipeline, no
INCR/EXPIRE from here), so asking "how much is left" cannot consume the budget it
asks about. Caps come from the very functions the worker enforces
(``budget.effective_cap``, ``budget._aggregate_cap``, ``safety_config.read_limit_for``
— no second formula), counters from the very Redis keys the worker writes
(per-account write ``budget:acct:…``, api_id aggregate ``budget:api:…``, read
``read:…``; the ``rate:`` prefix is added by redis_client).

Redis key shape drift is pinned byte-for-byte by tests/unit/test_limits_route.py
(§5 of the E1 acceptance): the f-strings below are the ONLY key builders on the
read side; if the worker's shapes move, that test breaks.
"""
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Query
from redis.exceptions import RedisError
from sqlalchemy import select

from app.api import deps
from app.core import safety_config
from app.core.clock import get_clock
from app.core.safety_defaults import READ_LIMITS
from app.db.models import Account, ApiCredential
from app.db.redis_client import rate_limit_peek_many
from app.services import budget

router = APIRouter(tags=["limits"])

CAP_PROFILE = "conservative"  # the one profile execution passes (base_task.py:351)


@router.get("/v1/limits")
async def get_limits(
    db: deps.GetDB,
    redis: deps.GetRedis,
    api_key: deps.VerifyAPIKey,
    account_ids: Annotated[Optional[str], Query(description="CSV целых чисел: ?account_ids=1,2,3; без параметра — все аккаунты")] = None,
):
    """Remaining daily budgets per account (read-only snapshot for Radar).

    Response: ``{"generated_at": ISO-8601 UTC, "accounts": [...], "missing": [...]}``.
    ``accounts`` carries one element per account; ``missing`` lists requested ids that
    have no row (a vanished id never fails the whole snapshot — Radar polls its whole
    fleet at once). Always 200 for well-formed input; Redis being down is 503 with no
    partial data (caps without spent would be a витрина that lies in both halves).

    Rules baked into every ``actions`` element (contract E1 §2):

    1. ``remaining`` — the SMALLER of the two ceilings (per-account and aggregate;
       no aggregate → per-account); ``binding`` names the binding ceiling. Plan on
       ``remaining``, never on ``per_account.remaining`` — same stricter-of semantics
       as the worker's ``budget.decide``.
    2. ``aggregate`` is non-null ONLY for SENSITIVE_ACTIONS (messages_per_day,
       joins_per_day, invites_per_day); for the other eight actions it is null and
       ``binding`` is "per_account".
    3. ``resets_in_seconds`` is the Redis key's TTL — the SLIDING 24 h window from
       the first spend, NOT the time to UTC midnight. The two magnitudes must never
       be merged into one number, on screen or in the client.
    4. No counter yet (key absent): ``used: 0``, ``remaining: cap``,
       ``resets_in_seconds: null``.
    5. Cap 0 (action forbidden for the use_case, e.g. invites_per_day under
       public_reply) still appears, with cap/used/remaining 0 — a "do not order"
       sign, never silently omitted.

    Errors: non-integer in ``account_ids`` → 422; unknown ids → 200 + ``missing``;
    Redis unreachable → 503 ``{"detail": "redis unavailable"}`` (whole request, no
    partial body).
    """
    # --- parse & normalise the optional CSV (any non-integer token → 422) ----------
    requested: list[int] = []
    if account_ids is not None:
        for chunk in account_ids.split(","):
            try:
                requested.append(int(chunk.strip()))
            except ValueError:
                raise HTTPException(
                    status_code=422, detail="account_ids: ожиданы целые числа"
                )
    seen_ids: set[int] = set()
    ids = [i for i in requested if not (i in seen_ids or seen_ids.add(i))]

    # --- accounts + their api credentials (one query; a dangling credential row is
    # tolerated exactly like the worker tolerates it, base_task.py:339-340) ---------
    stmt = select(Account, ApiCredential).outerjoin(
        ApiCredential, Account.api_credential_id == ApiCredential.id
    )
    if ids:
        stmt = stmt.where(Account.id.in_(ids))
    rows = list((await db.execute(stmt)).all())
    rows.sort(key=lambda row: row[0].id)
    found_ids = {account.id for account, _cred in rows}
    missing = [i for i in ids if i not in found_ids]

    # --- fixed action order: 4 write budgets (WRITE_BUDGET_ACTION order), then the
    # 7 reads (READ_LIMITS key order). Imported, not retyped: the dashboard must not
    # drift from what the worker actually budgets. Lazy import keeps pyrogram (via
    # base_task → _tg_errors) out of the gateway's import graph, as in accounts.py.
    from app.workers.base_task import WRITE_BUDGET_ACTION

    actions_order = [(action, "write") for action in WRITE_BUDGET_ACTION.values()]
    actions_order += [(action, "read") for action in READ_LIMITS]

    # --- first pass: per-account context + every Redis key we will need -------------
    # keys[] is deduplicated; aggregate keys are per (api_id, use_case, action) — the
    # E6 shape — so accounts sharing api_id AND use_case read the same counter once.
    keys: list[str] = []
    seen_keys: set[str] = set()

    def _add_key(key: str) -> None:
        if key not in seen_keys:
            seen_keys.add(key)
            keys.append(key)

    context = []  # (account, api_id, members, [(action, kind, per_key, agg_key|None)])
    for account, cred in rows:
        api_id = cred.api_id if cred else account.api_credential_id
        members = cred.account_count if cred and cred.account_count else 1
        per_action = []
        for action, kind in actions_order:
            if kind == "write":
                per_key = f"budget:acct:{account.id}:{action}"
            else:
                per_key = f"read:{action}:{account.id}"
            _add_key(per_key)
            agg_key = None
            if action in budget.SENSITIVE_ACTIONS:
                agg_key = f"budget:api:{api_id}:{account.use_case}:{action}"
                _add_key(agg_key)
            per_action.append((action, kind, per_key, agg_key))
        context.append((account, api_id, members, per_action))

    # --- ONE peek for the whole snapshot; Redis down → 503, no partial answer ------
    try:
        peeked_list = await rate_limit_peek_many(redis, keys)
    except RedisError:
        raise HTTPException(status_code=503, detail="redis unavailable")
    peeked = dict(zip(keys, peeked_list))

    # --- second pass: assemble the response -----------------------------------------
    accounts_out = []
    for account, _api_id, members, per_action in context:
        actions_out = []
        for action, kind, per_key, agg_key in per_action:
            if kind == "write":
                cap = budget.effective_cap(CAP_PROFILE, account.use_case, action, False)
            else:
                # Same read_limits section the worker's gate reads
                # (base_task.py:228 → get_read_limits) — one source, no drift.
                cap = safety_config.read_limit_for(action) or 0
            used, resets = peeked[per_key]
            per_account = {
                "cap": cap,
                "used": used,
                "remaining": max(0, cap - used),
                "resets_in_seconds": resets,
            }

            aggregate = None
            if agg_key is not None:
                # The worker's own aggregate-cap formula (budget.py:67-75), reached
                # through the worker's function — a second implementation here would
                # be the same kind of gap E3 fixed in config. `members` mirrors the
                # worker's account_count fallback (base_task.py:340).
                agg_cap = budget._aggregate_cap(
                    CAP_PROFILE, account.use_case, action, members
                )
                agg_used, agg_resets = peeked[agg_key]
                aggregate = {
                    "scope": "api_credential",
                    "api_credential_id": account.api_credential_id,
                    # The counter is keyed per use_case since E6; the cap is computed
                    # from the asking account's use_case, so the field must travel
                    # with the numbers for them to be interpretable.
                    "use_case": account.use_case,
                    "account_count": members,
                    "cap": agg_cap,
                    "used": agg_used,
                    "remaining": max(0, agg_cap - agg_used),
                    "resets_in_seconds": agg_resets,
                }

            decision = budget.decide(
                per_account["remaining"],
                None if aggregate is None else aggregate["remaining"],
            )
            remaining = (
                min(per_account["remaining"], aggregate["remaining"])
                if aggregate is not None
                else per_account["remaining"]
            )
            actions_out.append(
                {
                    "action": action,
                    "kind": kind,
                    "per_account": per_account,
                    "aggregate": aggregate,
                    "binding": decision.binding,
                    "remaining": remaining,
                }
            )

        accounts_out.append(
            {
                "account_id": account.id,
                "use_case": account.use_case,
                "api_credential_id": account.api_credential_id,
                "cap_profile": CAP_PROFILE,
                "actions": actions_out,
            }
        )

    return {
        "generated_at": get_clock().now().isoformat(),
        "accounts": accounts_out,
        "missing": missing,
    }
