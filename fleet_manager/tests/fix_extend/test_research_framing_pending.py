"""TDD markers for the research-framing requirements (FR-140–145, M0–M2).

These features do not exist yet → SKIP with explicit "pending" reasons (never a
silent pass). They become real assertions as each lands.
"""
import importlib

import pytest

from _helpers import PROJECT_ROOT, read_source


def _importorskip(modname: str):
    try:
        return importlib.import_module(modname)
    except ModuleNotFoundError:
        pytest.skip(f"PENDING (FR): module '{modname}' not implemented yet (002 / M0–M2).")


# --- FR-141: no implicit media download -------------------------------------------
def test_no_media_autodownload_in_update_handler():
    src = read_source("watchers/update_handler.py")
    assert "download_media" not in src or "MEDIA_AUTODOWNLOAD" in src, (
        "update_handler must not implicitly download_media(); metadata-only by default "
        "(FR-141). Add MEDIA_AUTODOWNLOAD gating or remove the implicit call."
    )


# --- FR-143: behaviour/ban telemetry store ----------------------------------------
def test_telemetry_event_model_present():
    models = importlib.import_module("app.db.models")
    assert hasattr(models, "TelemetryEvent"), (
        "TelemetryEvent table is the research instrument (per-account survival, ban "
        "cause, action log, warmup params) — FR-143."
    )


# --- FR-145: safety params are hot config, not hardcoded constants ----------------
def test_safety_params_loaded_from_config_not_constant():
    p = PROJECT_ROOT / "config" / "safety.yaml"
    if not p.exists():
        pytest.skip("PENDING (FR-145): config/safety.yaml (hot-reloadable warmup/limits) absent.")
    body = p.read_text()
    assert "reactions" in body and "cold_dm" in body, (
        "safety.yaml must hold the warmup schedules + limits as data (FR-145)."
    )


# --- FR-144: MCP usable by an autonomous agent ------------------------------------
def test_mcp_exposes_observe_and_status_for_agent():
    mod = _importorskip("app.mcp.server")
    # an agent must be able to observe inbound + query status without a human per action
    text = read_source("mcp/server.py") if (PROJECT_ROOT / "app" / "mcp" / "server.py").exists() else ""
    for tool in ("get_task_status", "get_account", "get_fleet_status"):
        assert tool in text, f"MCP must expose {tool} so an LLM agent can self-drive (FR-144)."
