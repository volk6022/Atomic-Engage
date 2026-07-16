"""Adaptive FloodWait escalation (feature 003, FR-351).

Repeated FloodWaits for the same account within a short window signal that the static
wait is too short; each successive FloodWait multiplies the effective wait. The count
of prior FloodWaits in the window is tracked in Redis (Clock-scaled TTL) by the worker;
this module holds the pure, unit-tested math.
"""
from __future__ import annotations

# A second+ FloodWait for the same account inside this many virtual seconds escalates.
ESCALATION_WINDOW_SECONDS = 3600
ESCALATION_FACTOR = 2.0
# Telegram's documented maximum FloodWait (24 h); never wait longer than this.
MAX_WAIT_SECONDS = 86400


def escalated_wait(
    base_wait: float,
    prior_floods_in_window: int,
    *,
    factor: float = ESCALATION_FACTOR,
    cap: int = MAX_WAIT_SECONDS,
) -> int:
    """Effective wait given how many FloodWaits already occurred in the window.

    ``prior_floods_in_window == 0`` -> base; ``1`` -> base×factor; ``2`` -> base×factor²,
    clamped to ``cap``.
    """
    n = max(0, prior_floods_in_window)
    return int(min(cap, base_wait * (factor ** n)))
