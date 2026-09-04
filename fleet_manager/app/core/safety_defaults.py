"""Default ban-safety parameters (warmup schedules + rate limits).

These are the **defaults** behind the hot-reloadable `config/safety.yaml` (FR-145).
The values mirror the authoritative warmup schedule in `data-model.md §9` (FR-110):
per-tier `days` are durations whose per-use-case sum is the total
(reactions 7 / join_groups 14 / cold_dm 30 / inviting 45).

This module is pure data with no project imports, so both `safety_config` (the
loader) and `services.warmup` (which re-exports `WARMUP_SCHEDULES` for the FR-110
test) can import it without a cycle.
"""

TIER_ORDER = ["fresh", "basic", "intermediate", "ready"]

# Every behavioural action, for the service_testing bench profile below.
_BENCH_ACTIONS = [
    "profile_setup", "subscribe_channels", "read", "forward",
    "react", "join_group", "invite_to_group", "cross_message_reply", "send_message",
]

WARMUP_SCHEDULES = {
    "reactions": {
        "total_days": 7,
        "tiers": {
            "fresh":        {"days": 1, "actions": ["profile_setup", "subscribe_channels", "read"]},
            "basic":        {"days": 2, "actions": ["react", "read", "subscribe_channels"]},
            "intermediate": {"days": 2, "actions": ["react", "forward", "read"]},
            "ready":        {"days": 2, "actions": ["react", "send_message"]},
        },
    },
    "join_groups": {
        "total_days": 14,
        "tiers": {
            "fresh":        {"days": 2, "actions": ["profile_setup", "read"]},
            "basic":        {"days": 4, "actions": ["react", "read", "subscribe_channels"]},
            "intermediate": {"days": 4, "actions": ["join_group", "react", "read"]},
            "ready":        {"days": 4, "actions": ["join_group", "send_message"]},
        },
    },
    "cold_dm": {
        "total_days": 30,
        "tiers": {
            "fresh":        {"days": 5, "actions": ["profile_setup", "read", "subscribe_channels"]},
            "basic":        {"days": 8, "actions": ["react", "read", "cross_message_reply"]},
            "intermediate": {"days": 10, "actions": ["cross_message_reply", "react", "read"]},
            "ready":        {"days": 7, "actions": ["send_message", "react"]},
        },
    },
    "inviting": {
        "total_days": 45,
        "tiers": {
            "fresh":        {"days": 7, "actions": ["profile_setup", "read", "subscribe_channels"]},
            "basic":        {"days": 12, "actions": ["react", "cross_message_reply", "read"]},
            "intermediate": {"days": 14, "actions": ["join_group", "react", "cross_message_reply"]},
            "ready":        {"days": 12, "actions": ["invite_to_group", "send_message"]},
        },
    },
    # Public replies in a channel's discussion group. The ladder copies join_groups
    # (14 days, a shape already run on live accounts) and differs in one place: the
    # `ready` tier adds send_message, because this use-case has to keep joining new
    # discussion groups AND post in them, while join_groups only ever joins.
    #
    # `cross_message_reply` is deliberately absent: it is a warmup-ladder concept, not
    # an API action — the gateway whitelist has no such action. A public reply IS a
    # send_message with reply_to_message_id into the discussion group.
    "public_reply": {
        "total_days": 14,
        "tiers": {
            "fresh":        {"days": 2, "actions": ["profile_setup", "read", "subscribe_channels"]},
            "basic":        {"days": 4, "actions": ["react", "read", "subscribe_channels"]},
            "intermediate": {"days": 4, "actions": ["join_group", "react", "read"]},
            "ready":        {"days": 4, "actions": ["join_group", "react", "send_message"]},
        },
    },
    # Bench profile: zero-day tiers. A use-case absent from this map makes
    # advance_tier_if_due return None, so an account left in `warmup` would never
    # graduate; a zero budget promotes one tier per tick, reaching `ready` instead.
    #
    # Every tier permits every action on purpose: a bench account is
    # created `fresh` and nothing promotes it before the first warmup_tick, so a
    # tier-gated action list would make the profile refuse the very actions a bench
    # run exists to exercise (join_group was rejected 409 this way). Ban-safety for
    # this use_case rests on the daily caps below, not on the warmup ladder.
    "service_testing": {
        "total_days": 0,
        "tiers": {
            tier: {"days": 0, "actions": _BENCH_ACTIONS}
            for tier in ("fresh", "basic", "intermediate", "ready")
        },
    },
}

# Per-use-case daily caps (research R5 / KB-derived defaults; tune per cohort in
# safety.yaml as Telegram anti-fraud evolves). 0 means the action is not part of
# that use-case's normal behaviour.
RATE_LIMITS = {
    "reactions":   {"reactions_per_day": 50, "messages_per_day": 0,  "joins_per_day": 0, "invites_per_day": 0,  "resolves_per_day": 100},
    "join_groups": {"reactions_per_day": 30, "messages_per_day": 0,  "joins_per_day": 5, "invites_per_day": 0,  "resolves_per_day": 100},
    "cold_dm":     {"reactions_per_day": 30, "messages_per_day": 20, "joins_per_day": 0, "invites_per_day": 0,  "resolves_per_day": 100},
    "inviting":    {"reactions_per_day": 30, "messages_per_day": 10, "joins_per_day": 3, "invites_per_day": 20, "resolves_per_day": 100},
    # Public replies. Joins are the scarcer axis (3/d, below join_groups' 5) because
    # this account also posts, and "joined ten groups today" is the pattern that gets
    # looked at. Messages are half of cold_dm's 20: a public reply carries no
    # message-to-non-contact signal, but it is visible to everyone in the group, so a
    # repetitive one is easier for a human to notice and report than a cold DM is.
    "public_reply": {"reactions_per_day": 30, "messages_per_day": 10, "joins_per_day": 3, "invites_per_day": 0,  "resolves_per_day": 100},
    # Bench profile — nominal ceilings so a budget never silently absorbs a test
    # result. Deliberately unsafe for real outreach; see UseCase.SERVICE_TESTING.
    "service_testing": {"reactions_per_day": 1000, "messages_per_day": 1000, "joins_per_day": 1000, "invites_per_day": 1000, "resolves_per_day": 1000},
}

# Cap PROFILES (feature 003, FR-340). `conservative` == the RATE_LIMITS above
# (current defaults, unchanged). `mature` lifts the over-throttled reaction/join axes
# toward the KB norm bands (reactions 150-250/d, joins 20-40/d) for aged, clean-CQS
# accounts — recovering the 5-10x under-use the limits audit flagged (defect #4).
RATE_LIMIT_PROFILES = {
    "conservative": RATE_LIMITS,
    "mature": {
        "reactions":   {"reactions_per_day": 200, "messages_per_day": 0,  "joins_per_day": 0,  "invites_per_day": 0,  "resolves_per_day": 150},
        "join_groups": {"reactions_per_day": 200, "messages_per_day": 0,  "joins_per_day": 30, "invites_per_day": 0,  "resolves_per_day": 150},
        "cold_dm":     {"reactions_per_day": 150, "messages_per_day": 30, "joins_per_day": 0,  "invites_per_day": 0,  "resolves_per_day": 150},
        "inviting":    {"reactions_per_day": 150, "messages_per_day": 15, "joins_per_day": 20, "invites_per_day": 40, "resolves_per_day": 150},
        "public_reply": {"reactions_per_day": 150, "messages_per_day": 20, "joins_per_day": 15, "invites_per_day": 0,  "resolves_per_day": 150},
        # Same nominal ceilings under `mature`: a missing use-case here resolves to
        # an empty dict and every cap reads 0, which would block the bench profile
        # entirely the moment the fleet is switched to this profile.
        "service_testing": {"reactions_per_day": 1000, "messages_per_day": 1000, "joins_per_day": 1000, "invites_per_day": 1000, "resolves_per_day": 1000},
    },
}

# Per-action KB ceilings (function-limits-cross-repo §3). An `is_premium` account
# doubles its profile caps (FR-341) but never exceeds the premium ceiling here.
PREMIUM_CEILINGS = {
    "messages_per_day": 100,   # DM to non-contacts
    "invites_per_day": 70,
    "joins_per_day": 200,
    "reactions_per_day": 1000,
    "resolves_per_day": 200,   # resolveUsername, 2y+ account
}

# Per-account daily caps for READ-only research actions (docs/research-agent-actions.md
# §4.1, contract §5). Reads are warmup-exempt (no behavioural footprint) but still
# budgeted so a bursty enrichment/monitoring run can never flood the fleet.
#
# get_chat_history was raised 80 -> 2000: a live measurement showed ~9000 messages/day
# in one active discussion group alone; at 80 calls/day x 50 msgs/call, backfilling a
# single group's past year would have taken two years.
# Discovery reads (get_similar_channels, search_public_chats) sit at 50: a crawl
# seeds one call per starting channel, and discovery is bursty by nature — a small
# ceiling keeps one enrichment run from eating the fleet's daily read budget in an
# hour. search_public_chat (singular) was removed: it named an action that never
# existed; the plural action carries this cap instead.
READ_LIMITS = {
    "resolve_username": 100,
    "get_chat_info": 200,
    "get_chat_history": 2000,
    "get_similar_channels": 50,
    "search_public_chats": 50,
    "get_chat_admins": 100,
    "get_dialogs": 50,
}
