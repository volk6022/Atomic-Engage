"""Meta-tests enforcing Constitution Principle VII / FR-130–135 on the test suite itself.

These pin the test-integrity defects found in review (T1–T3). Red until the legacy
conftest and task_dispatch suite are repaired.
"""
import pathlib

import app

PROJECT_ROOT = pathlib.Path(list(app.__path__)[0]).resolve().parent
LEGACY_CONFTEST = PROJECT_ROOT / "tests" / "conftest.py"
TASK_DISPATCH = PROJECT_ROOT / "tests" / "integration" / "test_task_dispatch.py"


# --- T1 / FR-130: fixtures must not swallow infra errors into a silent pass --------
def test_t1_legacy_conftest_does_not_swallow_infra_errors():
    src = LEGACY_CONFTEST.read_text()
    assert "yield None" not in src, (
        "tests/conftest.py yields None on infra failure (`except Exception: yield None`), "
        "so integration tests pass with NO database — proven by a dead-DB run yielding "
        "9/15 'passed'. Fixtures must skip explicitly instead (T1 / FR-130)."
    )


# --- T2 / FR-131: integration schema must come from Alembic, not create_all --------
def test_t2_legacy_conftest_uses_alembic_not_create_all():
    src = LEGACY_CONFTEST.read_text()
    assert ".create_all(" not in src and "alembic" in src.lower(), (
        "tests/conftest.py builds the schema with Base.metadata.create_all, so the "
        "partitioned peer_access_hashes is never exercised. Use `alembic upgrade head` "
        "(T2 / FR-131)."
    )


# --- T3 / FR-132: the missing Story-1 username-resolution scenario must exist -------
def test_t3_username_resolution_round_robin_scenario_exists():
    src = TASK_DISPATCH.read_text() if TASK_DISPATCH.exists() else ""
    assert "test_s1_sc3_username_resolution_round_robin" in src, (
        "Spec Story 1 sc3 (username resolution round-robin, FR-009) has no acceptance "
        "test; the FloodWait test is mislabelled sc3. Add sc3 and rename flood → sc4 "
        "(T3 / FR-132)."
    )
