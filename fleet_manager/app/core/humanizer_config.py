"""Humanizer & schedule configuration with defaults (feature 003, FR-332).

Every field has a default (the ported soverein values from data-model.md §3/§4), so
a missing or partial config section can never crash a worker — defaults apply and an
override hot-reloads through ``safety_config``.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class HumanizerConfig(BaseModel):
    """Ranges/weights for the micro-humanizer (data-model.md §4). All seconds are
    *virtual* and get compressed by the Clock at sleep time."""

    wpm: int = 200                      # reading speed (words/min)
    cpm: int = 300                      # typing speed (chars/min)

    reading_min_s: float = 1.5
    reading_max_s: float = 8.0

    typing_min_s: float = 2.0
    typing_max_s: float = 10.0
    typo_rate: float = Field(default=0.02, ge=0.0, le=0.05)  # 1-3% band, default 2%

    scroll_per_message_s: float = 0.4
    scroll_pause_chance: float = 0.15

    burst_every_n: int = 12
    burst_min_s: float = 300.0
    burst_max_s: float = 900.0

    reaction_min_s: float = 1.0
    reaction_max_s: float = 5.0

    inter_action_base_s: float = 300.0
    inter_action_jitter: float = 0.40   # +-40%
    inter_action_floor_s: float = 60.0

    # Weighted random-pause bands: (probability, low_s, high_s).
    random_pause_micro: tuple[float, float, float] = (0.50, 0.5, 1.5)
    random_pause_short: tuple[float, float, float] = (0.30, 1.5, 4.0)
    random_pause_medium: tuple[float, float, float] = (0.15, 4.0, 10.0)
    random_pause_long: tuple[float, float, float] = (0.05, 10.0, 30.0)


class ScheduleConfig(BaseModel):
    """Weekly schedule bounds (data-model.md §3, FR-320)."""

    work_start_min_h: int = 9
    work_start_max_h: int = 10
    work_end_min_h: int = 19
    work_end_max_h: int = 21
    minute_granularity: list[int] = Field(default_factory=lambda: [0, 15, 30, 45])
    mandatory_rest_days: int = 1
    second_rest_day_prob: float = 0.30
    regenerate_every_days: int = 7
    schedule_cache_ttl_s: int = 300
    dm_override_window_s: int = 20 * 60
