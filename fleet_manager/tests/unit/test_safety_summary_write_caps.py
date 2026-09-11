"""E2: `GET /v1/admin/safety` must report the enforced write-cap NUMBERS, not just
the use_case names.

Before E2 the summary's only write-side entry was `rate_limit_use_cases` — a bare
name list — so an operator who edits `rate_limits` in safety.yaml and POSTs
/v1/admin/reload-safety had no way to confirm from the response what the enforced
per-account caps now are. The new `rate_limits` key reports, per use_case, exactly
what the worker enforces: `safety_config.rate_limit_for_profile("conservative", ...)`
— the same function `budget.effective_cap` consults at every spend (E3 routes yaml
`rate_limits` edits into that table). The subject of these tests is the SAMENESS of
the two numbers: what the summary says == what the worker will enforce.

Pure unit tests: config loading + cap math only, no Postgres/Redis needed.
"""
import pytest

from app.core import safety_config, safety_defaults
from app.core.config import settings
from app.services import budget


@pytest.fixture(autouse=True)
def _reset_cache():
    # _ensure_loaded caches globally; every test must start from a clean slate.
    safety_config._cache = None
    safety_config._source = None
    yield
    safety_config._cache = None
    safety_config._source = None


def test_summary_rate_limits_match_enforced_caps_after_yaml_edit(
    tmp_path, monkeypatch
):
    """Harness §5.1: a tmp yaml raising public_reply.joins_per_day to 4 (path handed
    over through SAFETY_CONFIG_PATH, exactly as a deployment points the loader at a
    non-default file) must surface the SAME number 4 both in the summary the admin
    endpoint serves and in the cap the worker actually enforces."""
    yaml_path = tmp_path / "safety.yaml"
    yaml_path.write_text(
        "rate_limits:\n"
        "  public_reply: {joins_per_day: 4}\n",
        encoding="utf-8",
    )
    # _config_path resolves SAFETY_CONFIG_PATH (app/core/config.py:19) — patch the
    # singleton it reads rather than bypassing that resolution code.
    monkeypatch.setattr(settings, "SAFETY_CONFIG_PATH", str(yaml_path))

    summary = safety_config.reload()

    assert summary["rate_limits"]["public_reply"]["joins_per_day"] == 4
    assert (
        budget.effective_cap("conservative", "public_reply", "joins_per_day", False)
        == 4
    )
    # And not just that one number: every reported use_case table is identical to
    # the enforced one — one source of truth, no second formula.
    for use_case in summary["rate_limits"]:
        assert summary["rate_limits"][use_case] == (
            safety_config.rate_limit_for_profile("conservative", use_case)
        ), use_case


def test_summary_rate_limits_fall_back_to_code_defaults_without_yaml(
    tmp_path, monkeypatch
):
    """Harness §5.2: with _config_path pointing into the void, the summary must carry
    the code-default RATE_LIMITS verbatim, all six use_cases present."""
    monkeypatch.setattr(safety_config, "_config_path", lambda: tmp_path / "nope.yaml")

    summary = safety_config.active_summary()

    assert summary["source"] == "defaults"
    assert summary["rate_limits"] == safety_defaults.RATE_LIMITS
    assert set(summary["rate_limits"].keys()) == set(safety_defaults.RATE_LIMITS.keys())
    # The new numbers key covers exactly the use_cases the names key already lists.
    assert sorted(summary["rate_limits"].keys()) == summary["rate_limit_use_cases"]


def test_existing_five_summary_keys_keep_their_shape(tmp_path, monkeypatch):
    """Harness §5.3: Radar reads warmup_totals off this response, so the five pre-E2
    keys must be untouched — same values, same form — with and without an operator
    yaml; exactly one key (rate_limits) is added."""
    monkeypatch.setattr(safety_config, "_config_path", lambda: tmp_path / "nope.yaml")
    summary = safety_config.active_summary()

    assert set(summary.keys()) == {
        "source",
        "use_cases",
        "warmup_totals",
        "rate_limit_use_cases",
        "read_limits",
        "rate_limits",
    }  # the five existing keys plus exactly one new one
    assert summary["use_cases"] == sorted(safety_defaults.WARMUP_SCHEDULES.keys())
    assert summary["warmup_totals"] == {
        uc: spec["total_days"] for uc, spec in safety_defaults.WARMUP_SCHEDULES.items()
    }
    assert summary["rate_limit_use_cases"] == sorted(safety_defaults.RATE_LIMITS.keys())
    assert summary["read_limits"] == dict(safety_defaults.READ_LIMITS)

    # An operator yaml edit still leaves the five alone (source now names the path).
    yaml_path = tmp_path / "safety.yaml"
    yaml_path.write_text(
        "rate_limits:\n"
        "  public_reply: {joins_per_day: 4}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(safety_config, "_config_path", lambda: yaml_path)
    summary = safety_config.reload()

    assert summary["source"] == str(yaml_path)
    assert summary["use_cases"] == sorted(safety_defaults.WARMUP_SCHEDULES.keys())
    assert summary["warmup_totals"] == {
        uc: spec["total_days"] for uc, spec in safety_defaults.WARMUP_SCHEDULES.items()
    }
    assert summary["rate_limit_use_cases"] == sorted(safety_defaults.RATE_LIMITS.keys())
    assert summary["read_limits"] == dict(safety_defaults.READ_LIMITS)
    # ... while the new key reflects the edit.
    assert summary["rate_limits"]["public_reply"]["joins_per_day"] == 4
