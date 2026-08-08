"""Unit tests for the safety.yaml merge fix (B4c).

Before this fix, `_load_from_disk` REPLACED an entire section (`cfg["read_limits"] =
data["read_limits"]`) whenever the yaml mentioned it at all. Once `config/safety.yaml`
started actually being loaded (Dockerfile now ships it), that meant a yaml declaring
only one read_limits key silently deleted every other key — and the checked-in yaml
predates `service_testing`, so it would vanish from warmup_schedules/rate_limits
entirely, breaking the service_testing account used for real-Telegram acceptance runs.
"""
import pytest

from app.core import safety_config, safety_defaults


@pytest.fixture(autouse=True)
def _reset_cache():
    # _ensure_loaded caches globally; every test must start from a clean slate.
    safety_config._cache = None
    safety_config._source = None
    yield
    safety_config._cache = None
    safety_config._source = None


def test_merge_dict_keeps_untouched_sibling_keys():
    base = {"a": 1, "b": 2}
    merged = safety_config._merge_dict(base, {"a": 99})

    assert merged == {"a": 99, "b": 2}
    assert base == {"a": 1, "b": 2}  # base must not be mutated in place


def test_merge_dict_recurses_two_levels_for_rate_limit_profiles():
    # rate_limit_profiles is profile -> use_case -> caps: a yaml overriding one
    # use_case under one profile must not drop the profile's other use_cases.
    base = {
        "conservative": {
            "cold_dm": {"messages_per_day": 20},
            "reactions": {"reactions_per_day": 50},
        }
    }
    override = {"conservative": {"cold_dm": {"messages_per_day": 99}}}

    merged = safety_config._merge_dict(base, override)

    assert merged["conservative"]["cold_dm"]["messages_per_day"] == 99
    assert merged["conservative"]["reactions"]["reactions_per_day"] == 50


def test_load_from_disk_partial_read_limits_keeps_other_keys(tmp_path, monkeypatch):
    yaml_path = tmp_path / "safety.yaml"
    yaml_path.write_text("read_limits:\n  get_chat_history: 12345\n", encoding="utf-8")
    monkeypatch.setattr(safety_config, "_config_path", lambda: yaml_path)

    cfg, _source = safety_config._load_from_disk()

    assert cfg["read_limits"]["get_chat_history"] == 12345
    for action, cap in safety_defaults.READ_LIMITS.items():
        if action != "get_chat_history":
            assert cfg["read_limits"][action] == cap, action


def test_load_from_disk_yaml_without_service_testing_keeps_default(tmp_path, monkeypatch):
    """A yaml predating service_testing (only reactions/join_groups/cold_dm/inviting)
    must not make service_testing disappear from warmup_schedules or rate_limits."""
    yaml_path = tmp_path / "safety.yaml"
    yaml_path.write_text(
        "warmup_schedules:\n"
        "  reactions:\n"
        "    total_days: 7\n"
        "    tiers:\n"
        "      fresh: {days: 1, actions: [profile_setup]}\n"
        "rate_limits:\n"
        "  reactions: {reactions_per_day: 999}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(safety_config, "_config_path", lambda: yaml_path)

    cfg, _source = safety_config._load_from_disk()

    assert cfg["warmup_schedules"]["service_testing"] == (
        safety_defaults.WARMUP_SCHEDULES["service_testing"]
    )
    assert cfg["rate_limits"]["service_testing"] == safety_defaults.RATE_LIMITS["service_testing"]
    # the partial reactions override applied ...
    assert cfg["rate_limits"]["reactions"]["reactions_per_day"] == 999
    # ... without deleting reactions' other tiers.
    assert cfg["warmup_schedules"]["reactions"]["tiers"]["ready"] == (
        safety_defaults.WARMUP_SCHEDULES["reactions"]["tiers"]["ready"]
    )


def test_get_read_limits_reflects_merged_config(tmp_path, monkeypatch):
    yaml_path = tmp_path / "safety.yaml"
    yaml_path.write_text("read_limits:\n  get_chat_admins: 7\n", encoding="utf-8")
    monkeypatch.setattr(safety_config, "_config_path", lambda: yaml_path)

    limits = safety_config.get_read_limits()

    assert limits["get_chat_admins"] == 7
    assert limits["get_chat_history"] == safety_defaults.READ_LIMITS["get_chat_history"]
