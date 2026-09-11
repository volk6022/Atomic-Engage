"""E3: the yaml `rate_limits` section must be the operator knob for the
`conservative` cap profile.

Enforcement reads `rate_limit_profiles` (budget.effective_cap / budget._aggregate_cap
← safety_config.rate_limit_for_profile), while operator edits land in the
`rate_limits` section of safety.yaml and are applied by POST /v1/admin/reload-safety.
Before E3 the two were loaded independently: an operator could change `rate_limits`
and reload, the admin summary would happily report the new numbers, yet the enforced
`conservative` table stayed frozen at the code copy in
safety_defaults.RATE_LIMIT_PROFILES. These tests pin the closed gap: after any load
or reload, whatever `rate_limits` says is exactly what the `conservative` profile —
and therefore the enforced per-account and aggregate caps — uses.
"""
import pytest

from app.core import safety_config, safety_defaults
from app.services import budget


@pytest.fixture(autouse=True)
def _reset_cache():
    # _ensure_loaded caches globally; every test must start from a clean slate.
    safety_config._cache = None
    safety_config._source = None
    yield
    safety_config._cache = None
    safety_config._source = None


def test_conservative_profile_still_equals_code_defaults_without_yaml(
    tmp_path, monkeypatch
):
    # An empty directory (no safety.yaml at all): the conservative profile must keep
    # carrying exactly the code-default caps — E3 must not move the floor.
    monkeypatch.setattr(safety_config, "_config_path", lambda: tmp_path / "nope.yaml")

    for use_case, caps in safety_defaults.RATE_LIMITS.items():
        assert safety_config.rate_limit_for_profile("conservative", use_case) == caps, (
            use_case
        )


def test_yaml_rate_limits_override_reaches_enforced_conservative_profile(
    tmp_path, monkeypatch
):
    """The gap itself: an operator raising/lowering a cap in `rate_limits` must change
    what the worker enforces, not only what the admin summary reports."""
    yaml_path = tmp_path / "safety.yaml"
    yaml_path.write_text(
        "rate_limits:\n"
        "  cold_dm: {messages_per_day: 7}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(safety_config, "_config_path", lambda: yaml_path)

    cfg, _source = safety_config._load_from_disk()

    assert cfg["rate_limit_profiles"]["conservative"]["cold_dm"]["messages_per_day"] == 7
    assert safety_config.rate_limit_for_profile("conservative", "cold_dm") == {
        "reactions_per_day": 30,
        "messages_per_day": 7,
        "joins_per_day": 0,
        "invites_per_day": 0,
        "resolves_per_day": 100,
    }
    # Through the pure cap math the worker actually calls:
    assert budget.effective_cap("conservative", "cold_dm", "messages_per_day", False) == 7


def test_yaml_override_keeps_sibling_keys_and_use_cases(tmp_path, monkeypatch):
    # The per-key merge (and the binding built on top of it) must not let a one-line
    # operator edit erase anything the code defaults provide.
    yaml_path = tmp_path / "safety.yaml"
    yaml_path.write_text(
        "rate_limits:\n"
        "  cold_dm: {messages_per_day: 7}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(safety_config, "_config_path", lambda: yaml_path)

    cfg, _source = safety_config._load_from_disk()

    # Untouched cap inside the overridden use_case survives.
    assert cfg["rate_limits"]["cold_dm"]["resolves_per_day"] == (
        safety_defaults.RATE_LIMITS["cold_dm"]["resolves_per_day"]
    )
    # Untouched use_cases survive verbatim — including service_testing, which the
    # checked-in yaml predates in older deployments.
    for use_case in safety_defaults.RATE_LIMITS:
        if use_case != "cold_dm":
            assert (
                cfg["rate_limit_profiles"]["conservative"][use_case]
                == safety_defaults.RATE_LIMITS[use_case]
            ), use_case


def test_mature_profile_is_not_touched_by_rate_limits_override(tmp_path, monkeypatch):
    # `rate_limits` is the knob for `conservative` only; the mature table stays put.
    yaml_path = tmp_path / "safety.yaml"
    yaml_path.write_text(
        "rate_limits:\n"
        "  cold_dm: {messages_per_day: 7}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(safety_config, "_config_path", lambda: yaml_path)

    cfg, _source = safety_config._load_from_disk()

    assert (
        cfg["rate_limit_profiles"]["mature"]["cold_dm"]
        == safety_defaults.RATE_LIMIT_PROFILES["mature"]["cold_dm"]
    )


def test_unknown_profile_falls_back_to_overridden_conservative(tmp_path, monkeypatch):
    # rate_limit_for_profile falls back to `conservative` for an unknown profile —
    # that fallback must see the yaml override too, not a stale table.
    yaml_path = tmp_path / "safety.yaml"
    yaml_path.write_text(
        "rate_limits:\n"
        "  cold_dm: {messages_per_day: 7}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(safety_config, "_config_path", lambda: yaml_path)

    safety_config._load_from_disk()

    assert (
        safety_config.rate_limit_for_profile("does_not_exist", "cold_dm")[
            "messages_per_day"
        ]
        == 7
    )


def test_aggregate_cap_follows_yaml_override(tmp_path, monkeypatch):
    # The api_id/subnet aggregate is derived from the same per-account cap; it must
    # move with the override (0.6 × cap × members), never below one account's cap.
    yaml_path = tmp_path / "safety.yaml"
    yaml_path.write_text(
        "rate_limits:\n"
        "  cold_dm: {messages_per_day: 7}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(safety_config, "_config_path", lambda: yaml_path)

    safety_config._load_from_disk()

    assert (
        budget._aggregate_cap("conservative", "cold_dm", "messages_per_day", 5) == 21
    )  # round(0.6 × 7 × 5)
    # A lone member is never throttled below its own per-account cap.
    assert (
        budget._aggregate_cap("conservative", "cold_dm", "messages_per_day", 1) == 7
    )


def test_reload_picks_up_edited_rate_limits(tmp_path, monkeypatch):
    # The operator workflow end-to-end: load once, edit the yaml on disk, reload —
    # the enforced conservative caps must follow without a process restart.
    yaml_path = tmp_path / "safety.yaml"
    yaml_path.write_text(
        "rate_limits:\n"
        "  public_reply: {joins_per_day: 3}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(safety_config, "_config_path", lambda: yaml_path)
    safety_config.reload()
    assert (
        safety_config.rate_limit_for_profile("conservative", "public_reply")[
            "joins_per_day"
        ]
        == 3
    )

    yaml_path.write_text(
        "rate_limits:\n"
        "  public_reply: {joins_per_day: 1}\n",
        encoding="utf-8",
    )
    safety_config.reload()

    assert (
        safety_config.rate_limit_for_profile("conservative", "public_reply")[
            "joins_per_day"
        ]
        == 1
    )
    assert (
        budget.effective_cap(
            "conservative", "public_reply", "joins_per_day", False
        )
        == 1
    )


def test_conservative_and_rate_limits_stay_one_table_after_reload(
    tmp_path, monkeypatch
):
    # The binding is one table, not a snapshot: every use_case the operator can edit
    # in `rate_limits` is visible under the conservative profile, identically.
    yaml_path = tmp_path / "safety.yaml"
    yaml_path.write_text(
        "rate_limits:\n"
        "  reactions: {reactions_per_day: 42}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(safety_config, "_config_path", lambda: yaml_path)
    safety_config.reload()

    profiles = safety_config.get_rate_limit_profiles()
    assert profiles["conservative"] == safety_config.get_rate_limits()
    assert profiles["conservative"]["reactions"]["reactions_per_day"] == 42
