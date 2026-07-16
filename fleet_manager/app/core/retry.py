"""Unified retry policy (feature 003, FR-353).

soverein scattered ``retry = 3`` (with ``delay = uniform(base, base*3)``) across four
call sites; divergent copies are a source of hard-to-trace behaviour differences.
This single object centralises attempts, base delay, the jitter band, and the set of
fatal (never-retried) error classes.
"""
from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_s: float = 10.0
    jitter: tuple[float, float] = (1.0, 3.0)  # multiplier band on base
    fatal_classes: tuple[type[BaseException], ...] = field(default_factory=tuple)

    def delay_for(self, attempt: int) -> float:
        """Jittered backoff for the given (1-based) attempt number."""
        lo, hi = self.jitter
        return self.base_s * random.uniform(lo, hi)

    def delays(self) -> Iterator[float]:
        """Yield the wait between each pair of attempts (``attempts - 1`` values)."""
        for attempt in range(1, self.attempts):
            yield self.delay_for(attempt)

    def should_retry(self, exc: BaseException, attempt: int) -> bool:
        """True if ``exc`` on (1-based) ``attempt`` is retryable and budget remains."""
        if isinstance(exc, self.fatal_classes):
            return False
        return attempt < self.attempts
