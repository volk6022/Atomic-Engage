"""Unit tests for cap profiles, premium multiplier, and aggregate decision logic
(FR-340/341/342, SC-005). The pure cap math is asserted here; the Redis consume path
is covered by the integration suite.
"""
from app.services import budget


def test_conservative_profile_matches_current_defaults():
    # cold_dm messages cap unchanged at 20/day.
    assert budget.effective_cap("conservative", "cold_dm", "messages_per_day", False) == 20
    assert budget.effective_cap("conservative", "reactions", "reactions_per_day", False) == 50


def test_mature_profile_lifts_reactions_and_joins_into_kb_band():
    react = budget.effective_cap("mature", "reactions", "reactions_per_day", False)
    joins = budget.effective_cap("mature", "join_groups", "joins_per_day", False)
    assert 150 <= react <= 250
    assert 20 <= joins <= 40


def test_premium_doubles_caps_clamped_to_ceiling():
    # cold_dm messages: 20 -> 40 (under the 100 ceiling).
    assert budget.effective_cap("conservative", "cold_dm", "messages_per_day", True) == 40
    # mature reactions: 200 -> 400 (under the 1000 ceiling).
    assert budget.effective_cap("mature", "reactions", "reactions_per_day", True) == 400


def test_premium_clamps_at_ceiling():
    # Force a value whose ×2 would exceed the ceiling; clamp wins.
    capped = budget.apply_premium("messages_per_day", 80, is_premium=True)  # 160 -> 100
    assert capped == 100


def test_stricter_of_per_account_and_aggregate_wins():
    d = budget.decide(per_account_remaining=5, aggregate_remaining=2)
    assert d.allowed is True
    assert d.binding == "aggregate"
    d2 = budget.decide(per_account_remaining=0, aggregate_remaining=10)
    assert d2.allowed is False
    assert d2.binding == "per_account"


def test_unknown_profile_falls_back_to_conservative():
    assert budget.effective_cap("does_not_exist", "cold_dm", "messages_per_day", False) == 20
