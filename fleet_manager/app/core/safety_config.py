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
    if isinstance(data.get("warmup_schedules"), dict) and data["warmup_schedules"]:
        cfg["warmup_schedules"] = data["warmup_schedules"]
    if isinstance(data.get("rate_limits"), dict) and data["rate_limits"]:
        cfg["rate_limits"] = data["rate_limits"]
    if isinstance(data.get("read_limits"), dict) and data["read_limits"]:
        cfg["read_limits"] = data["read_limits"]
    if isinstance(data.get("rate_limit_profiles"), dict) and data["rate_limit_profiles"]:
        cfg["rate_limit_profiles"] = data["rate_limit_profiles"]
    if isinstance(data.get("premium_ceilings"), dict) and data["premium_ceilings"]:
        cfg["premium_ceilings"] = data["premium_ceilings"]
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
    }
