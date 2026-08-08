"""Unit tests for get_chat_history's paging-cursor plumbing (§2 contract).

Pure-function tests only: `_effective_max_id` and `_MAX_LIMIT`. The full worker
(`get_chat_history`) goes through `run_task`, which needs a DB session — covered by
the existing `tests/fix_extend` integration suite, not here.
"""
import logging

from app.workers.get_chat_history import _MAX_LIMIT, _effective_max_id


def test_max_limit_is_1000():
    assert _MAX_LIMIT == 1000


def test_effective_max_id_uses_max_id_directly():
    assert _effective_max_id({"max_id": 500}, task_id=1) == 500


def test_effective_max_id_defaults_to_zero():
    assert _effective_max_id({}, task_id=1) == 0


def test_effective_max_id_translates_offset_id_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        result = _effective_max_id({"offset_id": 321}, task_id=42)

    assert result == 321
    assert "offset_id" in caplog.text and "deprecated" in caplog.text


def test_effective_max_id_prefers_explicit_max_id_over_offset_id():
    # Both given: max_id (the non-deprecated cursor) wins.
    assert _effective_max_id({"max_id": 100, "offset_id": 999}, task_id=1) == 100
