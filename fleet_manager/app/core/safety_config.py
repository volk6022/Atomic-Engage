"""Hot-reloadable ban-safety config (FR-145): warmup schedules + rate limits.

Loads from `config/safety.yaml` if present, otherwise falls back to the code
defaults in :mod:`app.core.safety_defaults`. Reloadable at runtime via
:func:`reload` (wired to ``POST /v1/admin/reload-safety``; on POSIX a ``SIGHUP``
handler is also installed in ``main.py``). Call sites read through the getters,
so a reload takes effect without a process restart — no safety value is a
hardcoded constant at the point of use.
"""
from __future__ import annotations

import copy
import logging
import pathlib
import threading
from typing import Optional

from app.core import safety_defaults

logger = logging.getLogger(__name__)

# this file is app/core/safety_config.py → parents[2] == fleet_manager/
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _PROJECT_ROOT / "config" / "safety.yaml"

_lock = threading.RLock()
_cache: Optional[dict] = None
_source: Optional[str] = None


def _config_path() -> pathlib.Path:
    """Resolve the safety.yaml path: explicit setting > default location."""
    p = ""
    try:
        from app.core.config import get_settings

        p = getattr(get_settings(), "SAFETY_CONFIG_PATH", "") or ""
    except Exception:  # noqa: BLE001 — settings optional during early import
        p = ""
    if p:
        pp = pathlib.Path(p)
        return pp if pp.is_absolute() else (_PROJECT_ROOT / pp)
    return _DEFAULT_PATH


def _defaults() -> dict:
    return {
        "warmup_schedules": copy.deepcopy(safety_defaults.WARMUP_SCHEDULES),
        "rate_limits": copy.deepcopy(safety_defaults.RATE_LIMITS),
        "read_limits": copy.deepcopy(safety_defaults.READ_LIMITS),
        "rate_limit_profiles": copy.deepcopy(safety_defaults.RATE_LIMIT_PROFILES),
        "premium_ceilings": copy.deepcopy(safety_defaults.PREMIUM_CEILINGS),
    }


def _merge_dict(base: dict, override: dict) -> dict:
    """Shallow-merge `override` onto a copy of `base`, key by key.

    A key present in `base` but absent from `override` survives untouched — the whole
    point of this function. Plain `cfg[section] = data[section]` (the old behaviour)
    instead REPLACED the section wholesale, so a yaml that only mentions one key
    silently deleted every other key that section used to have (e.g. `service_testing`
    disappearing from `rate_limits` because the checked-in yaml predates that
    use_case). Non-dict values just overwrite, same as a normal `dict.update`.
    """
    if not isinstance(override, dict):
        return override
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge_dict(existing, value)
        else:
            merged[key] = value
    return merged


def _load_from_disk() -> tuple[dict, str]:
    cfg = _defaults()
    path = _config_path()
    if not path.exists():
        return cfg, "defaults"
    try:
        import yaml

        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001 — bad config must never crash a worker
        logger.error("safety_config_load_failed path=%s err=%s; using defaults", path, exc)
        return cfg, "defaults(load_error)"
    # Merge onto the defaults PER KEY (see _merge_dict) rather than replacing each
    # section wholesale: a yaml that only overrides one use_case/action must not erase
    # every other one that only exists in code (defaults are the floor, not optional).
    # rate_limit_profiles is a dict of dicts (profile -> use_case -> caps), so it needs
    # the same key-wise treatment two levels deep; _merge_dict already recurses for that.
    for section in (
        "warmup_schedules",
        "rate_limits",
        "read_limits",
        "rate_limit_profiles",
        "premium_ceilings",
    ):
        override = data.get(section)
        if isinstance(override, dict) and override:
            cfg[section] = _merge_dict(cfg[section], override)
    # E3: the yaml `rate_limits` section is the OPERATOR KNOB for the `conservative`
    # cap profile — the profile the worker actually enforces (base_task passes
    # cap_profile="conservative" and budget.effective_cap/_aggregate_cap read it via
    # rate_limit_for_profile). Enforcement consults `rate_limit_profiles`, so without
    # this binding an operator edit + POST /v1/admin/reload-safety changed only what
    # get_rate_limits()/admin summary reported while the enforced caps stayed frozen
    # at the code copy. Binding (sharing the object, not copying) restores the alias
    # the code defaults already express (safety_defaults.RATE_LIMIT_PROFILES maps
    # "conservative" to the very same RATE_LIMITS object); every consumer only reads
    # these dicts. The defaults/load_error paths above skip this line, but there
    # `cfg["rate_limits"]` and the conservative table are deep copies of the same
    # constants, so they are equal by construction. `mature` and any profile tables
    # merged from an explicit yaml `rate_limit_profiles` section keep their own data;
    # `conservative` is deliberately *defined* by `rate_limits`, but an operator who
    # also writes an explicit `rate_limit_profiles.conservative` keeps its unique
    # keys -- `rate_limits` only wins the conflicts. A plain alias would drop that
    # section silently even though the loader above merges it.
    cfg["rate_limit_profiles"]["conservative"] = _merge_dict(
        cfg["rate_limit_profiles"]["conservative"], cfg["rate_limits"]
    )
    return cfg, str(path)


def _ensure_loaded() -> dict:
    global _cache, _source
    with _lock:
        if _cache is None:
            _cache, _source = _load_from_disk()
            logger.info("safety_config_loaded source=%s", _source)
        return _cache


def reload() -> dict:
    """Force a re-read from disk; returns the active-config summary."""
    global _cache, _source
    with _lock:
        _cache, _source = _load_from_disk()
        logger.info("safety_config_reloaded source=%s", _source)
    return active_summary()


def get_warmup_schedules() -> dict:
    return _ensure_loaded()["warmup_schedules"]


def get_rate_limits() -> dict:
    return _ensure_loaded()["rate_limits"]


def rate_limit_for(use_case: str) -> dict:
    return _ensure_loaded()["rate_limits"].get(use_case, {})


def get_read_limits() -> dict:
    """Per-account daily caps for read-only research actions (§4.1)."""
    return _ensure_loaded()["read_limits"]


def get_rate_limit_profiles() -> dict:
    """Named cap profiles (`conservative`/`mature`) — feature 003, FR-340."""
    return _ensure_loaded()["rate_limit_profiles"]


def rate_limit_for_profile(profile: str, use_case: str) -> dict:
    """Per-use-case caps for a profile, falling back to `conservative` then {}."""
    profiles = _ensure_loaded()["rate_limit_profiles"]
    table = profiles.get(profile) or profiles.get("conservative") or {}
    return table.get(use_case, {})


def get_premium_ceilings() -> dict:
    """Per-action KB ceilings clamping the is_premium ×2 multiplier (FR-341)."""
    return _ensure_loaded()["premium_ceilings"]


def read_limit_for(action: str) -> Optional[int]:
    return _ensure_loaded()["read_limits"].get(action)


def active_summary() -> dict:
    cfg = _ensure_loaded()
    ws = cfg["warmup_schedules"]
    return {
        "source": _source,
        "use_cases": sorted(ws.keys()),
        "warmup_totals": {uc: v.get("total_days") for uc, v in ws.items()},
        "rate_limit_use_cases": sorted(cfg["rate_limits"].keys()),
        # Effective read caps, verbatim. The summary used to name only the write-side
        # use_cases, so the one number an operator most often needs to confirm after a
        # reload -- "did my read budget actually take?" -- was unanswerable over the API.
        "read_limits": dict(cfg["read_limits"]),
        # E2: numbers, not names. The write side used to expose only the use_case list
        # above, so an operator who edits `rate_limits` in safety.yaml and reloads could
        # not confirm from the API what the enforced caps now are. Each table is the
        # effective per-account write caps for the profile the worker actually enforces
        # ("conservative" is hardcoded at the call site, base_task cap_profile=...),
        # read through the very rate_limit_for_profile() call budget.effective_cap
        # consults at every spend -- no second formula, no direct yaml read here (that
        # would recreate the E3 gap inside the summary). These are NOT aggregate caps:
        # an aggregate depends on api_id/account_count and lives in /v1/limits (E1).
        "rate_limits": {
            use_case: dict(rate_limit_for_profile("conservative", use_case))
            for use_case in sorted(cfg["rate_limits"].keys())
        },
    }
