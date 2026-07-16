"""Unit tests for the ported micro-humanizer (FR-330/331/332/333, SC-006).

Pure compute functions are asserted directly (fast, no real sleeping); the async
wrappers only add a scaled Clock.sleep on top of these numbers.
"""
import random
from collections import Counter

import pytest

from app.core.clock import Clock
from app.core.humanizer_config import HumanizerConfig
from app.services.humanizer import Humanizer


@pytest.fixture
def hz():
    return Humanizer(HumanizerConfig(), Clock(time_scale=1.0))


def test_defaults_apply_without_config():
    """A missing/partial config must not crash; Pydantic defaults apply (FR-332)."""
    cfg = HumanizerConfig()
    assert cfg.reading_min_s == 1.5
    assert cfg.typing_max_s == 10.0
    assert 0.0 < cfg.typo_rate <= 0.05
    # Partial override hot-path: unknown-absent fields keep defaults.
    cfg2 = HumanizerConfig(typo_rate=0.03)
    assert cfg2.typo_rate == 0.03
    assert cfg2.reading_min_s == 1.5


def test_reading_delay_in_range_and_scales(hz):
    random.seed(1)
    for words in (1, 50, 100, 1000):
        s = hz.reading_seconds(words)
        assert hz.cfg.reading_min_s <= s <= hz.cfg.reading_max_s
    # Scaling: a 48x clock compresses the *real* sleep but not the virtual figure.
    fast = Humanizer(HumanizerConfig(), Clock(time_scale=48.0))
    assert fast.clock.scaled_sleep_seconds(fast.reading_seconds(100)) < hz.reading_seconds(100)


def test_typing_trace_typo_band_and_nonconstant_intervals(hz):
    random.seed(7)
    text = "x" * 200
    trace = hz.typing_trace(text)
    assert hz.cfg.typing_min_s <= trace.total_s <= hz.cfg.typing_max_s
    # 1-3% typo band over 200 chars => roughly 2-6 typos; allow [1, 10] for variance.
    assert 1 <= trace.typo_count <= 10
    rate = trace.typo_count / len(text)
    assert 0.0 < rate <= 0.05
    # Inter-key intervals must be non-constant (linear typing is a bot signal).
    assert len(set(round(i, 6) for i in trace.inter_key_intervals)) > 1


def test_burst_delay_long_every_n(hz):
    random.seed(3)
    n = hz.cfg.burst_every_n
    short = hz.burst_seconds(1)
    long = hz.burst_seconds(n)
    assert hz.cfg.burst_min_s <= long <= hz.cfg.burst_max_s
    assert short < hz.cfg.burst_min_s


def test_reaction_and_inter_action_ranges(hz):
    random.seed(11)
    for _ in range(50):
        r = hz.reaction_seconds()
        assert hz.cfg.reaction_min_s <= r <= hz.cfg.reaction_max_s
        ia = hz.inter_action_seconds()
        assert ia >= hz.cfg.inter_action_floor_s


def test_random_pause_weighted_distribution(hz):
    random.seed(123)
    bands = Counter(hz.random_pause_band() for _ in range(4000))
    total = sum(bands.values())
    # micro 50% / short 30% / medium 15% / long 5% within tolerance.
    assert abs(bands["micro"] / total - 0.50) < 0.06
    assert abs(bands["short"] / total - 0.30) < 0.06
    assert abs(bands["medium"] / total - 0.15) < 0.05
    assert abs(bands["long"] / total - 0.05) < 0.04


def test_poisson_delay_bounds(hz):
    random.seed(5)
    for _ in range(200):
        d = hz.poisson_delay(mean=3.0, lo=0.5, hi=10.0)
        assert 0.5 <= d <= 10.0
