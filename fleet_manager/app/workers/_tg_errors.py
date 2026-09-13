"""Telegram (kurigram/pyrogram) error groupings used by worker error handling.

Imported defensively so a kurigram version that renames/removes a class does not
break import of the workers.
"""
import asyncio

from pyrogram import errors

FloodWait = errors.FloodWait
PeerIdInvalid = getattr(errors, "PeerIdInvalid", None)


# Transport-level failures that mean "this proxy exit is bad / unreachable right now",
# NOT a Telegram-side rejection. These trigger proxy-port rotation + retry rather than
# a hard task failure. ConnectionError/TimeoutError subclass OSError, but we list them
# for clarity; python-socks proxy errors are added defensively if importable.
_CONN_TYPES = [OSError, asyncio.TimeoutError, TimeoutError, ConnectionError]
try:  # python-socks raises its own ProxyError family for proxy-handshake failures
    from python_socks import ProxyError, ProxyConnectionError, ProxyTimeoutError

    _CONN_TYPES += [ProxyError, ProxyConnectionError, ProxyTimeoutError]
except Exception:  # noqa: BLE001 — best-effort; OSError already covers most cases
    pass

CONNECTION_ERRORS = tuple(dict.fromkeys(_CONN_TYPES))

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


# ---------------------------------------------------------------------------
# Last-resort classification (task 3.3): split what fell through run_task's
# named branches into programmer mistakes vs unknown Telegram rejections vs
# the rest. NO existing code is renamed here (FLOOD_WAIT / CANCELLED_BY_RADAR /
# PROXY_ROTATED / JOB_TIMEOUT / ban-codes stay as they are) — only new codes
# are added, so Radar's code-lookup keeps working.
PROGRAMMER_ERRORS: tuple[type[BaseException], ...] = (
    AttributeError,
    TypeError,
    KeyError,
    IndexError,
    NameError,
    AssertionError,
    NotImplementedError,
    UnboundLocalError,
)


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """Map an exception that fell through run_task's named branches to
    (error_code, log level).

    Does NOT classify FloodWait / BAN_ERRORS / CONNECTION_ERRORS (nor
    PeerIdInvalid): base_task.run_task catches those earlier with named except
    branches and routes each to its own recovery (defer / ban / proxy rotation),
    so they never reach the generic ``except Exception``. Calling this directly
    with one of them yields their RPCError/name fallback — a caller bug, not a
    valid route.

    Returns (code, level):
      * ("PROGRAMMER_ERROR", "error")  — a broken invariant in OUR code; retry
        cannot help, log at ERROR with the stack trace, emit a telemetry event.
      * ("TG_RPC", "warning")          — any other kurigram/pyrogram RPCError
        not matched by a named branch: a Telegram-side rejection the catalogue
        does not know yet; log at WARNING with class + text.
      * (type(exc).__name__, "warning") — anything else keeps the previous
        behaviour so no information is lost.
    """
    if isinstance(exc, PROGRAMMER_ERRORS):
        return "PROGRAMMER_ERROR", "error"
    if isinstance(exc, errors.RPCError):
        return "TG_RPC", "warning"
    return type(exc).__name__, "warning"
