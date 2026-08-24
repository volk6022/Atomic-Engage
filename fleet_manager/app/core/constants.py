from enum import StrEnum


class AccountStatus(StrEnum):
    WARMUP = "warmup"
    ACTIVE = "active"
    FLOOD = "flood"
    SLEEPING = "sleeping"
    BANNED = "banned"


class WarmupTier(StrEnum):
    FRESH = "fresh"
    BASIC = "basic"
    INTERMEDIATE = "intermediate"
    READY = "ready"


class TaskType(StrEnum):
    SEND_MESSAGE = "send_message"
    JOIN_GROUP = "join_group"
    REACT = "react"
    RESOLVE_USERNAME = "resolve_username"
    INVITE_TO_GROUP = "invite_to_group"
    WARMUP_ACTION = "warmup_action"
    GET_CHAT_INFO = "get_chat_info"
    GET_CHAT_HISTORY = "get_chat_history"
    GET_CHAT_ADMINS = "get_chat_admins"
    GET_DIALOGS = "get_dialogs"


# Read-only research lookups (docs/research-agent-actions.md §4.1): public-entity
# reads with no behavioural footprint. They are EXEMPT from the warmup gate (like the
# original lone resolve_username exemption) but still read-budget limited.
READ_ACTIONS = frozenset(
    {
        TaskType.RESOLVE_USERNAME,
        TaskType.GET_CHAT_INFO,
        TaskType.GET_CHAT_HISTORY,
        TaskType.GET_CHAT_ADMINS,
        TaskType.GET_DIALOGS,
    }
)


class UseCase(StrEnum):
    REACTIONS = "reactions"
    JOIN_GROUPS = "join_groups"
    COLD_DM = "cold_dm"
    INVITING = "inviting"
    # Public replies in a channel's discussion group. Needs BOTH join_group (the
    # comments live in a linked group the account must be a member of) and
    # send_message (the reply itself). No existing profile grants both: cold_dm can
    # message but never joins, join_groups joins but its messages_per_day is 0.
    PUBLIC_REPLY = "public_reply"
    # Bench profile for exercising the fleet end to end: nominal 1000/day on every
    # per-use-case axis so budgets never mask what is being tested. NOT for real
    # outreach -- these caps are far above anything Telegram tolerates sustained.
    SERVICE_TESTING = "service_testing"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    EXECUTING = "executing"
    COMPLETE = "complete"
    FAILED = "failed"
    DEFERRED = "deferred"
