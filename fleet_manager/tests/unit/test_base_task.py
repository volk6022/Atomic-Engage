"""Unit tests for BaseTask.prepare() covering all early-return paths."""
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
    account = _make_account(phone_country="RU", proxy_country="US")
    db = _make_db(account=account)

    with respx_mock_ctx():
        result = await BaseTask.prepare({}, 1, db)

    assert result is None
    assert account.status == AccountStatus.SLEEPING
    db.commit.assert_awaited()


def respx_mock_ctx():
    import respx
    import httpx
    mock = respx.mock(assert_all_mocked=False)
    mock.post("https://your-n8n-instance.com/webhook/fleet").mock(
        return_value=httpx.Response(200)
    )
    return mock


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
