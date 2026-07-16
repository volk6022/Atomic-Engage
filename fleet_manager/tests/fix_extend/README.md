# `fix_extend` — TDD red phase for spec `002-fix-extend`

These tests are written **test-first** (Constitution Principle VII). They are
**expected to fail or skip** against the current `001` code — that is the point.
Each one turns green only when the paired `FR-1xx` repair from
`specs/002-fix-extend/` is implemented (implementation is intentionally NOT done
in the review deliverable).

| File | Role | Current state |
|---|---|---|
| `test_001_defects.py` | Pins the **verified** 001 defects (C1–C9). | RED (defects present) |
| `test_test_integrity.py` | Pins test-suite integrity defects (T1–T3). | RED |
| `test_new_features_pending.py` | Marks new v2.1 features (MCP, recovery, backups, fleet_service). | SKIP (pending) |
| `conftest.py` | **Honest** fixtures: skip-with-reason when infra absent, never silent pass (contrast with legacy `tests/conftest.py`). | — |

Run: `pytest tests/fix_extend -v`.

**Phase 3 landed (2026-06-04)** — US2 account & proxy lifecycle is real. `C4`
(onboarding persists, preserving imported Desktop fingerprint + api_id per FR-146) and
`C6` (accounts/proxies/api-credentials routers registered + auth-protected) are GREEN.
New `test_onboarding_real.py` (real DB + real routes): onboarding persists / geo-mismatch
creates nothing / datacenter rejected / batch 1:1 proxies / reserve pool / api-credential
dup 409 / unban / reactivate (health monkeypatched) / get / proxy reassign. `parse_proxy_url`
rewritten on urlparse (socks5 fix). Coverage made greenlet-aware (SQLAlchemy async bridge):
TOTAL ~86%, api modules ≥83%. Remaining sub-80% (`geo_match` GeoIP-reader, `proxy_manager`
health-loop) are network/GeoIP boundaries for later phases.

**Phase 2 landed (2026-06-04)** — US1 dispatch loop executes end-to-end. `C1` (worker
ctx has session_maker), `C2` (workers delegate to the shared `run_task` orchestrator),
`C3` (no `import settings`) are GREEN. New genuine worker-runtime tests
(`test_worker_runtime.py`, real DB + faked kurigram) cover: send/complete, FIFO,
FloodWait→flood/defer, ban→banned, PEER_ID_INVALID→delete+re-resolve, generic failure,
off-hours defer, startup recovery, deferred scheduler, and every worker type.
`app/workers` coverage ≈95%. Remaining failures are US2 (accounts/proxies stubs +
unregistered routers → C4/C6), US6 (warmup schedule → C9), and telemetry (FR-143).

**Phase 1 landed (2026-06-04)** — these red tests are now GREEN:
`test_c7` (Alembic initial migration exists), `test_c8` (peer_access_hashes composite
PK, partition-ready), `test_t1`/`test_t2` (conftest no longer swallows infra / uses
Alembic), `test_t3` (the missing `test_s1_sc3_username_resolution_round_robin` was
added; flood test renamed `sc4`). Remaining failures (C1/C2/C3/C4/C6/C9, telemetry)
and skips (MCP, recovery, backups, hot-config) are the next phases.

Coverage target for the *implementation* phase: **≥80 % per module** on
`services/ workers/ watchers/ api/`, gate enforced (`--cov-fail-under=80` plus a
per-package check). That gate cannot pass until the repairs land — these red
tests define the target.

> Note: because these live under the default `testpaths`, the aggregate suite is
> now (correctly) red. The previous "104 passed / 82 %" green was illusory — the
> legacy integration tests pass with no database (proven by a dead-DB run). Fixing
> `tests/conftest.py` (FR-130/131/134) is the first implementation task.
