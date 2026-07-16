"""Unit tests for FloodWait fallback + escalation math (FR-351)."""
import pytest

from app.core.floodwait import MAX_WAIT_SECONDS, escalated_wait


def test_unparseable_floodwait_fallback_is_300():
    # _tg_errors imports kurigram/pyrogram; skip cleanly where it isn't installed.
    tg = pytest.importorskip("app.workers._tg_errors")

    class _FW:
        value = None
        x = None

    assert tg.flood_seconds(_FW()) == 300
    assert tg.FLOODWAIT_FALLBACK_SECONDS == 300


def test_parseable_floodwait_uses_its_value():
    tg = pytest.importorskip("app.workers._tg_errors")

    class _FW:
        value = 42
        x = None

    assert tg.flood_seconds(_FW()) == 42


def test_escalation_doubles_each_time_in_window():
    assert escalated_wait(300, 0) == 300
    assert escalated_wait(300, 1) == 600
    assert escalated_wait(300, 2) == 1200


def test_escalation_clamped_to_max():
    assert escalated_wait(300, 20) == MAX_WAIT_SECONDS
