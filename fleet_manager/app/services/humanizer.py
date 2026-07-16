"""Micro-humanizer ported from soverein ``utils/jitter.py`` (feature 003).

Replaces the old linear ``typing = 0.1 s/char`` model (a bot signal) with a richer,
hot-reloadable set: reading/typing/scroll/Poisson/burst/random-pause + 1-3% typo
injection with backspace correction and stochastic inter-key intervals
(FR-330/331/333). All sleeps go through the injected :class:`Clock` so behavioural
delays compress with ``TIME_SCALE`` (FR-302).

The pure ``*_seconds`` / ``*_band`` / ``typing_trace`` helpers compute the (virtual)
figures and are unit-tested directly; the ``async *_delay`` wrappers add the scaled
sleep on top so tests never have to wait real minutes.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from app.core.clock import Clock
from app.core.humanizer_config import HumanizerConfig


@dataclass
class TypingTrace:
    """Observable result of a typing simulation (lets tests assert realism)."""

    total_s: float
    char_count: int
    typo_count: int
    inter_key_intervals: list[float] = field(default_factory=list)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class Humanizer:
    def __init__(self, cfg: HumanizerConfig | None = None, clock: Clock | None = None):
        self.cfg = cfg or HumanizerConfig()
        self.clock = clock or Clock()

    # ---- reading -----------------------------------------------------------------
    def reading_seconds(self, word_count: int) -> float:
        base = (max(0, word_count) / max(1, self.cfg.wpm)) * 60.0
        jittered = base * random.uniform(0.8, 1.25)
        return _clamp(jittered, self.cfg.reading_min_s, self.cfg.reading_max_s)

    async def reading_delay(self, word_count: int) -> None:
        await self.clock.sleep(self.reading_seconds(word_count))

    # ---- typing (with typo injection + stochastic inter-key) ---------------------
    def typing_trace(self, text: str) -> TypingTrace:
        n = len(text)
        if n == 0:
            return TypingTrace(total_s=0.0, char_count=0, typo_count=0)

        per_char = 60.0 / max(1, self.cfg.cpm)
        intervals: list[float] = []
        typo_count = 0
        for _ in range(n):
            # Real keystrokes vary; never a constant cadence.
            intervals.append(per_char * random.uniform(0.5, 1.8))
            if random.random() < self.cfg.typo_rate:
                # mistype -> notice -> backspace -> retype: three extra keystrokes.
                typo_count += 1
                intervals.append(per_char * random.uniform(0.5, 1.8))   # wrong char
                intervals.append(per_char * random.uniform(0.8, 2.0))   # backspace
                intervals.append(per_char * random.uniform(0.5, 1.8))   # correct char

        raw_total = sum(intervals)
        total = _clamp(raw_total, self.cfg.typing_min_s, self.cfg.typing_max_s)
        if raw_total > 0:
            scale = total / raw_total
            intervals = [i * scale for i in intervals]
        return TypingTrace(
            total_s=total, char_count=n, typo_count=typo_count, inter_key_intervals=intervals
        )

    async def typing_delay(self, text: str) -> TypingTrace:
        trace = self.typing_trace(text)
        await self.clock.sleep(trace.total_s)
        return trace

    # ---- scroll ------------------------------------------------------------------
    def scroll_seconds(self, message_count: int) -> float:
        secs = max(0, message_count) * self.cfg.scroll_per_message_s
        if random.random() < self.cfg.scroll_pause_chance:
            secs += random.uniform(1.0, 4.0)
        return secs

    async def scroll_delay(self, message_count: int) -> None:
        await self.clock.sleep(self.scroll_seconds(message_count))

    # ---- Poisson (memoryless) ----------------------------------------------------
    def poisson_delay(self, mean: float, lo: float, hi: float) -> float:
        if mean <= 0:
            return lo
        return _clamp(random.expovariate(1.0 / mean), lo, hi)

    # ---- burst -------------------------------------------------------------------
    def burst_seconds(self, action_counter: int) -> float:
        if action_counter > 0 and action_counter % self.cfg.burst_every_n == 0:
            return random.uniform(self.cfg.burst_min_s, self.cfg.burst_max_s)
        return random.uniform(0.5, 4.0)

    async def burst_delay(self, action_counter: int) -> None:
        await self.clock.sleep(self.burst_seconds(action_counter))

    # ---- weighted random pause ---------------------------------------------------
    def _bands(self) -> list[tuple[str, float, float, float]]:
        c = self.cfg
        return [
            ("micro", *c.random_pause_micro),
            ("short", *c.random_pause_short),
            ("medium", *c.random_pause_medium),
            ("long", *c.random_pause_long),
        ]

    def random_pause_band(self) -> str:
        bands = self._bands()
        return random.choices(
            [b[0] for b in bands], weights=[b[1] for b in bands], k=1
        )[0]

    def random_pause_seconds(self) -> float:
        name = self.random_pause_band()
        lo, hi = next((b[2], b[3]) for b in self._bands() if b[0] == name)
        return random.uniform(lo, hi)

    async def random_pause(self) -> None:
        await self.clock.sleep(self.random_pause_seconds())

    # ---- reaction & inter-action -------------------------------------------------
    def reaction_seconds(self) -> float:
        return random.uniform(self.cfg.reaction_min_s, self.cfg.reaction_max_s)

    async def reaction_delay(self) -> None:
        await self.clock.sleep(self.reaction_seconds())

    def inter_action_seconds(self) -> float:
        base = self.cfg.inter_action_base_s
        jitter = self.cfg.inter_action_jitter
        value = base * random.uniform(1.0 - jitter, 1.0 + jitter)
        return max(self.cfg.inter_action_floor_s, value)

    async def inter_action_delay(self) -> None:
        await self.clock.sleep(self.inter_action_seconds())
