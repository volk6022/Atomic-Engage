"""Unit tests for BaseTask.prepare() covering all early-return paths."""
import logging

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.workers.base_task import BaseTask
from app.core.constants import AccountStatus, TaskStatus


def _make_account(
    *,
    status="active",
    phone_country="RU",
    proxy_country="RU",
    flood_until=None,
    work_start=9,
    work_end=22,
    tz_offset=10800,
    geo_override=False,
):
    proxy = MagicMock()
    proxy.country = proxy_country

    account = MagicMock()
    account.status = status
    account.phone_country = phone_country
    account.proxy = proxy
    account.flood_until = flood_until
    account.work_start = work_start
    account.work_end = work_end
    account.proxy.tz_offset = tz_offset
    # A MagicMock attribute is truthy, so the geo-override branch would be
    # taken by accident; the double has to state the default explicitly.
    account.geo_override = geo_override
    return account


def _make_db(account=None, task=None):
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = account
    db.execute.return_value = result
    return db


@pytest.mark.asyncio
async def test_prepare_returns_none_when_account_not_found():
    db = _make_db(account=None)
    result = await BaseTask.prepare({}, 999, db)
    assert result is None


@pytest.mark.asyncio
async def test_prepare_returns_none_when_account_banned():
    account = _make_account(status=AccountStatus.BANNED)
    db = _make_db(account=account)
    result = await BaseTask.prepare({}, 1, db)
    assert result is None


@pytest.mark.asyncio
async def test_prepare_returns_none_when_flood_until_in_future():
    flood = datetime.now(timezone.utc) + timedelta(hours=3)
    account = _make_account(flood_until=flood)
    db = _make_db(account=account)
    result = await BaseTask.prepare({}, 1, db)
    assert result is None


@pytest.mark.asyncio
async def test_prepare_proceeds_when_flood_until_in_past():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    account = _make_account(flood_until=past)
    db = AsyncMock()

    account_result = MagicMock()
    account_result.scalar_one_or_none.return_value = account

    no_task_result = MagicMock()
    no_task_result.scalar_one_or_none.return_value = None

    db.execute.side_effect = [account_result, no_task_result, no_task_result]

    with patch("app.workers.base_task.working_hours.WorkingHoursGuard") as mock_guard:
        mock_guard.return_value.check.return_value = (True, None)
        result = await BaseTask.prepare({}, 1, db)

    assert result is not None
    assert result["account"] is account


@pytest.mark.asyncio
async def test_prepare_is_pure_gate_fifo_handled_by_run_task():
    # New design (FR-102): prepare is a pure account-runnability gate. Per-account
    # FIFO (one executing task at a time) is enforced in run_task, not prepare. So a
    # runnable account yields {"account": account} regardless of other executing tasks.
    account = _make_account()
    db = _make_db(account=account)

    with patch("app.workers.base_task.working_hours.WorkingHoursGuard") as mock_guard:
        mock_guard.return_value.check.return_value = (True, None)
        result = await BaseTask.prepare({}, 1, db)

    assert result == {"account": account}


@pytest.mark.asyncio
async def test_prepare_geo_reject_sets_sleeping_and_returns_none():
    """A geo mismatch sleeps the account and QUEUES the alert.

    This used to assert an inline HTTP POST. Delivery moved off the task path
    deliberately: awaiting it here meant an unreachable receiver held the worker for
    the full 450 s backoff, so the alert is now persisted and handed to
    `deliver_webhook` instead of being sent from inside the guard.
    """
    account = _make_account(phone_country="RU", proxy_country="US")
    db = _make_db(account=account)

    with patch("app.services.webhook_queue.enqueue_webhook") as mock_enqueue:
        mock_enqueue.return_value = 1
        result = await BaseTask.prepare({}, 1, db)

    assert result is None
    assert account.status == AccountStatus.SLEEPING
    db.commit.assert_awaited()
    mock_enqueue.assert_awaited_once()
    _, url, payload = mock_enqueue.await_args.args
    assert payload["event"] == "geo_reject"
    assert payload["account_id"] == 1


@pytest.mark.asyncio
async def test_prepare_returns_none_outside_working_hours():
    # New design: prepare returns None when outside the working window; run_task is
    # responsible for setting the task's deferred_until (verified in worker-runtime tests).
    account = _make_account()
    db = _make_db(account=account)

    deferred_until = datetime.now(timezone.utc) + timedelta(hours=8)
    with patch("app.workers.base_task.working_hours.WorkingHoursGuard") as mock_guard:
        mock_guard.return_value.check.return_value = (False, deferred_until)
        result = await BaseTask.prepare({}, 1, db)

    assert result is None


@pytest.mark.asyncio
async def test_prepare_returns_account_dict_on_happy_path():
    account = _make_account()

    db = AsyncMock()
    account_result = MagicMock()
    account_result.scalar_one_or_none.return_value = account
    no_task_result = MagicMock()
    no_task_result.scalar_one_or_none.return_value = None

    db.execute.side_effect = [account_result, no_task_result, no_task_result]

    with patch("app.workers.base_task.working_hours.WorkingHoursGuard") as mock_guard:
        mock_guard.return_value.check.return_value = (True, None)
        result = await BaseTask.prepare({}, 1, db)

    assert result == {"account": account}


@pytest.mark.asyncio
async def test_prepare_honours_geo_override_on_every_dispatch(caplog):
    """The override is deliberately wider than the pairing it was granted for.

    Read literally, `geo_override` says "this phone country may be paired with this
    proxy country", and it is set once, at onboarding. The gate it opens, however, is
    re-run before EVERY dispatch and again on reactivation, so one acknowledgement
    keeps the account running for the rest of its life. An audit raised that asymmetry
    as a finding; it was reviewed and kept on purpose (owner's decision, 2026-08-25).

    The reasoning, so the next audit does not reopen it: a country divergence puts the
    account itself at risk and nobody else. Whoever accepted the pairing accepted the
    work done over it. Re-asking per task would either stall the fleet on an absent
    operator or train them to confirm without reading, and a prompt nobody reads is
    worse protection than no prompt at all.

    What the decision does not license is an *invisible* exemption. Hence the assertion
    on the log line: it is the only trace that a gate was skipped for this account, and
    a refactor that quietly drops it should fail here.
    """
    account = _make_account(phone_country="RU", proxy_country="US", geo_override=True)
    db = _make_db(account=account)

    with caplog.at_level(logging.WARNING, logger="app.workers.base_task"):
        with patch("app.workers.base_task.working_hours.WorkingHoursGuard") as mock_guard:
            mock_guard.return_value.check.return_value = (True, None)
            result = await BaseTask.prepare({}, 1, db)

    assert result == {"account": account}
    assert account.status != AccountStatus.SLEEPING
    db.commit.assert_not_awaited()
    assert "geo_mismatch_overridden" in caplog.text
    assert "RU" in caplog.text and "US" in caplog.text
