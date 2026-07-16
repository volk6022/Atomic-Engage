"""TDD red tests pinning the VERIFIED defects in 001 (see reports/002-fleet-orchestrator-review.md).

These are expected to FAIL against the current code (red phase). Each turns green
only when the paired FR-1xx repair lands. They assert corrected behaviour, not the
current (broken) behaviour.
"""
import importlib

import pytest

from _helpers import read_source, PROJECT_ROOT


# --- C1 / FR-101: worker ARQ context must provide a DB session + Redis -------------
def test_c1_arq_on_startup_populates_ctx_db_and_redis():
    """on_startup must mutate ctx with a db session factory and redis (ARQ ignores
    the return value). Today it only `return {"redis": ...}` and never sets ctx['db']."""
    src = read_source("workers/arq_settings.py")
    # Workers read ctx["db"]; on_startup must place a session/session_maker into ctx.
    # ARQ ignores on_startup's return value, so a bare `return {...}` never reaches ctx.
    has_db_in_ctx = any(
        key in src for key in ('ctx["db"]', "ctx['db']", 'ctx["session_maker"]', "ctx['session_maker']")
    )
    assert has_db_in_ctx, (
        "arq_settings.on_startup must put a DB session/session_maker into `ctx` "
        "(workers do `db = ctx['db']`). Today it only `return {\"redis\": ...}`, which "
        "ARQ ignores, so every worker KeyErrors on first job (defect C1 / FR-101)."
    )


# --- C3 / FR-103: enqueue_next must not `import settings` --------------------------
def test_c3_enqueue_next_uses_app_config_not_bare_import_settings():
    import re

    src = read_source("workers/base_task.py")
    # Match an actual `import settings` statement, not prose mentioning it.
    assert not re.search(r"(?m)^\s*import settings\b", src), (
        "base_task.enqueue_next does `import settings` (no such top-level module). "
        "It must use app.core.config.get_settings() (defect C3 / FR-103)."
    )


# --- C2 / FR-102: every worker must call BaseTask.prepare() and enqueue_next() ------
# --- C2 / FR-102: every worker must run through the shared orchestrator that
#     enforces prepare() (guards/FIFO) and enqueue_next() ------------------------------
@pytest.mark.parametrize(
    "worker_module",
    [
        "send_message",
        "join_group",
        "react",
        "resolve_username",
        "invite_to_group",
        "warmup_action",
    ],
)
def test_c2_workers_invoke_prepare_and_enqueue_next(worker_module):
    src = read_source(f"workers/{worker_module}.py")
    assert "run_task(" in src, (
        f"{worker_module} must delegate to base_task.run_task (the single path that "
        f"enforces prepare()/FIFO/enqueue_next) — defect C2 / FR-102."
    )
    base = read_source("workers/base_task.py")
    assert "prepare(" in base and "enqueue_next(" in base, (
        "base_task.run_task must call BaseTask.prepare() and enqueue_next() (FR-102)."
    )


# --- C6 / FR-107: all real routers must be registered in main.py -------------------
# (webhook_events is a Pydantic *schemas* module, not a router — excluded.)
@pytest.mark.parametrize("router", ["accounts", "proxies", "api_credentials"])
def test_c6_main_registers_all_routers(router):
    src = read_source("main.py")
    assert f"{router}.router" in src or f"include_router({router}" in src, (
        f"main.py does not register the '{router}' router → its endpoints are 404 "
        f"(defect C6 / FR-107)."
    )


# --- C4 / FR-106: onboarding endpoint must not be a stub ---------------------------
def test_c4_create_account_is_not_a_stub():
    src = read_source("api/v1/accounts.py")
    # The stub literally `return {"id": 1, "status": "warmup"}` with no DB work.
    assert 'return {"id": 1, "status": "warmup"}' not in src, (
        "POST /v1/accounts is a stub returning a hardcoded body with no geo-validation, "
        "fingerprint, or persistence (defect C4 / FR-106)."
    )


# --- C7 / FR-108: an Alembic initial migration must exist --------------------------
def test_c7_alembic_initial_migration_exists():
    versions = PROJECT_ROOT / "migrations" / "versions"
    revs = list(versions.glob("*.py")) if versions.exists() else []
    assert revs, (
        "migrations/versions/ has no revision files; `alembic upgrade head` creates "
        "nothing and partitions never exist (defect C7 / FR-108)."
    )


# --- C8 / FR-109: peer_access_hashes must be partition-ready (composite PK) ---------
def test_c8_peer_access_hash_has_composite_pk_account_and_peer():
    models = importlib.import_module("app.db.models")
    pah = models.PeerAccessHash
    pk_cols = {c.name for c in pah.__table__.primary_key.columns}
    assert pk_cols == {"account_id", "peer_id"}, (
        f"PeerAccessHash PK is {pk_cols}; data-model §7 requires composite PK "
        f"(account_id, peer_id) with PARTITION BY RANGE(account_id) (defect C8 / FR-109)."
    )


# --- C9 / FR-110: warmup totals must match data-model §9 ---------------------------
def test_c9_warmup_schedule_totals_match_data_model():
    warmup = importlib.import_module("app.services.warmup")
    schedules = warmup.WARMUP_SCHEDULES
    expected_totals = {"reactions": 7, "join_groups": 14, "cold_dm": 30, "inviting": 45}

    def total_days(use_case_cfg) -> int:
        # Support both the (correct) per-tier 'days' model and any cumulative variant.
        tiers = use_case_cfg.get("tiers", use_case_cfg) if isinstance(use_case_cfg, dict) else use_case_cfg
        vals = []
        for cfg in tiers.values():
            d = getattr(cfg, "days", None)
            if d is None and isinstance(cfg, dict):
                d = cfg.get("days")
            if d is None:
                d = getattr(cfg, "days_min", None)
            vals.append(d or 0)
        # per-tier model → sum; cumulative model → max
        return max(sum(vals), max(vals) if vals else 0) if False else sum(vals)

    actual = {uc: total_days(cfg) for uc, cfg in schedules.items()}
    assert actual == expected_totals, (
        f"Warmup totals {actual} != data-model §9 {expected_totals}. "
        f"001 code uses 3/7/14/21 for every use_case (defect C9 / FR-110)."
    )
