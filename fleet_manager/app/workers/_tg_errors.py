"""Telegram (kurigram/pyrogram) error groupings used by worker error handling.

Imported defensively so a kurigram version that renames/removes a class does not
break import of the workers.
"""
from pyrogram import errors

FloodWait = errors.FloodWait
PeerIdInvalid = getattr(errors, "PeerIdInvalid", None)

# Any 401 means the session is no longer usable for this account → treat as banned.
# `Unauthorized` is the 401 base class (covers UserDeactivated, UserDeactivatedBan,
# AuthKeyUnregistered, SessionRevoked, SessionExpired, ...).
BAN_ERRORS = tuple(
    cls
    for cls in (
        getattr(errors, "Unauthorized", None),
        getattr(errors, "UserDeactivated", None),
        getattr(errors, "UserDeactivatedBan", None),
    )
    if cls is not None
)


# Read-only lookups against a handle that doesn't exist or isn't publicly readable.
# The research agent treats these as a clean "no data" (null), not a worker failure.
NOT_FOUND_ERRORS = tuple(
    cls
    for cls in (
        getattr(errors, "UsernameNotOccupied", None),
        getattr(errors, "UsernameInvalid", None),
        getattr(errors, "ChannelPrivate", None),
        getattr(errors, "ChannelInvalid", None),
        getattr(errors, "PeerIdInvalid", None),
    )
    if cls is not None
)


# Conservative fallback when a FloodWait carries no parseable duration (feature 003,
# FR-351 / C-1): raised from 60s to 300s — re-flooding after too short a wait escalates
# the ban, so erring long is safer.
FLOODWAIT_FALLBACK_SECONDS = 300


def flood_seconds(exc) -> int:
    """Seconds to wait for a FloodWait, defensively."""
    return int(
        getattr(exc, "value", None)
        or getattr(exc, "x", None)
        or FLOODWAIT_FALLBACK_SECONDS
    )
