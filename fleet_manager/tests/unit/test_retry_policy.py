"""Unit tests for the unified RetryPolicy (FR-353).

Consolidates the scattered ``retry=3`` literals into one object (attempts, base,
jitter band, fatal classes) read by all retry sites.
"""
import random

import pytest

from app.core.retry import RetryPolicy


def test_defaults_match_ported_values():
    p = RetryPolicy()
    assert p.attempts == 3
    assert p.base_s == 10.0
    assert p.jitter == (1.0, 3.0)


def test_delay_for_attempt_within_jitter_band():
    random.seed(0)
    p = RetryPolicy(base_s=10.0, jitter=(1.0, 3.0))
    for attempt in range(1, p.attempts + 1):
        d = p.delay_for(attempt)
        assert 10.0 <= d <= 30.0


def test_should_retry_respects_attempts_and_fatal():
    class Boom(Exception):
        pass

    class FatalBan(Exception):
        pass

    p = RetryPolicy(attempts=3, fatal_classes=(FatalBan,))
    assert p.should_retry(Boom(), attempt=1) is True
    assert p.should_retry(Boom(), attempt=3) is False   # no attempts left
    assert p.should_retry(FatalBan(), attempt=1) is False  # fatal, never retry


def test_delays_iterator_length():
    p = RetryPolicy(attempts=4)
    delays = list(p.delays())
    assert len(delays) == 3  # attempts-1 waits between 4 tries
