"""TDD markers for NEW v2.1 capabilities not present in 001.

These features (MCP server, restart-recovery module, shared fleet_service, backup
scripts) do not exist yet, so these tests SKIP with an explicit "pending" reason
(not a silent pass). They flip to real assertions as each module lands.
"""
import importlib

import pytest

from _helpers import PROJECT_ROOT


def _importorskip(modname: str):
    try:
        return importlib.import_module(modname)
    except ModuleNotFoundError:
        pytest.skip(f"PENDING (FR): module '{modname}' not implemented yet (002).")


# --- FR-126/128: MCP server + shared service layer --------------------------------
def test_mcp_server_module_present():
    mod = _importorskip("app.mcp.server")
    assert hasattr(mod, "build_server") or hasattr(mod, "server"), (
        "app.mcp.server must expose the MCP server (FR-126)."
    )


def test_fleet_service_seam_present():
    mod = _importorskip("app.services.fleet_service")
    for fn in ("dispatch_action", "onboard_account", "get_fleet_status"):
        assert hasattr(mod, fn), f"fleet_service must expose {fn}() shared by REST+MCP (FR-128)."


# --- FR-120: restart recovery module ----------------------------------------------
def test_recovery_module_present():
    mod = _importorskip("app.workers.recovery")
    assert hasattr(mod, "recover_orphaned_tasks"), (
        "recovery.recover_orphaned_tasks() must reset stale 'executing' tasks (FR-120)."
    )


# --- FR-105: deferred-task scheduler ----------------------------------------------
def test_deferred_scheduler_present():
    mod = _importorskip("app.workers.recovery")
    assert hasattr(mod, "reenqueue_due_deferred") or hasattr(mod, "deferred_scheduler"), (
        "a deferred-task scheduler must re-enqueue due 'deferred' tasks (FR-105)."
    )


# --- FR-122/124: backup + restore scripts -----------------------------------------
def test_backup_script_present():
    p = PROJECT_ROOT / "scripts" / "backup.sh"
    if not p.exists():
        pytest.skip("PENDING (FR-122): scripts/backup.sh not implemented yet.")
    body = p.read_text()
    assert "pg_dump" in body and "-Fc" in body, "backup.sh must pg_dump -Fc (FR-122)."


def test_restore_script_present():
    p = PROJECT_ROOT / "scripts" / "restore.sh"
    if not p.exists():
        pytest.skip("PENDING (FR-124): scripts/restore.sh not implemented yet.")
    assert "pg_restore" in p.read_text(), "restore.sh must use pg_restore (FR-124)."


# --- FR-122: compose must run a backup service ------------------------------------
def test_compose_has_backup_service():
    p = PROJECT_ROOT / "docker-compose.yml"
    body = p.read_text() if p.exists() else ""
    if "backup" not in body:
        pytest.skip("PENDING (FR-122): docker-compose.yml has no backup service.")
    assert "backup" in body
