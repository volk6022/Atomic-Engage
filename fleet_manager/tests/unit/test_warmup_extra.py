"""Additional warmup tests covering edge-case branches."""
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.warmup import WarmupPipeline, WARMUP_SCHEDULES
from app.core.constants import WarmupTier


@pytest.fixture
def pipeline():
    return WarmupPipeline()


def _mock_account(tier="basic", use_case="reactions", warmup_day=5, status="warmup"):
    account = MagicMock()
    account.warmup_tier = tier
    account.use_case = use_case
    account.warmup_day = warmup_day
    account.status = status
    account.id = 1
    return account


# ── get_allowed_actions edge cases ───────────────────────────────────────────

def test_get_allowed_actions_unknown_use_case_returns_empty(pipeline):
    account = _mock_account(use_case="nonexistent")
    assert pipeline.get_allowed_actions(account) == []


def test_get_allowed_actions_unknown_tier_returns_empty(pipeline):
    account = _mock_account(tier="unknown_tier", use_case="reactions")
    assert pipeline.get_allowed_actions(account) == []


# ── advance_tier_if_due early returns ────────────────────────────────────────

@pytest.mark.asyncio
async def test_advance_tier_if_due_negative_warmup_day_returns_none(pipeline):
    account = _mock_account(warmup_day=-1)
    result = await pipeline.advance_tier_if_due(account, AsyncMock(), MagicMock())
    assert result is None


@pytest.mark.asyncio
async def test_advance_tier_if_due_ready_tier_returns_none(pipeline):
    account = _mock_account(tier=WarmupTier.READY, warmup_day=99)
    result = await pipeline.advance_tier_if_due(account, AsyncMock(), MagicMock())
    assert result is None


@pytest.mark.asyncio
async def test_advance_tier_if_due_unknown_tier_returns_none(pipeline):
    account = _mock_account(tier="unknown_tier", warmup_day=99)
    result = await pipeline.advance_tier_if_due(account, AsyncMock(), MagicMock())
    assert result is None


@pytest.mark.asyncio
async def test_advance_tier_if_due_not_enough_days_returns_none(pipeline):
    # reactions completes 'basic' at cumulative day 3 (fresh 1 + basic 2, FR-110);
    # warmup_day=2 is not enough to advance out of basic.
    account = _mock_account(tier="basic", use_case="reactions", warmup_day=2)
    result = await pipeline.advance_tier_if_due(account, AsyncMock(), MagicMock())
    assert result is None


@pytest.mark.asyncio
async def test_advance_tier_if_due_advances_to_ready_sets_active(pipeline):
    import respx, httpx
    # reactions intermediate requires 14 days
    account = _mock_account(tier="intermediate", use_case="reactions", warmup_day=21)

    db = AsyncMock()

    with respx.mock(assert_all_mocked=False) as mock:
        mock.post("https://your-n8n-instance.com/webhook/fleet").mock(
            return_value=httpx.Response(200)
        )
        result = await pipeline.advance_tier_if_due(account, db, MagicMock())

    assert result == WarmupTier.READY
    from app.core.constants import AccountStatus
    assert account.status == AccountStatus.ACTIVE


# ── select_cross_pair ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_select_cross_pair_returns_none_when_no_candidates(pipeline):
    db = AsyncMock()
    pairs_result = MagicMock()
    pairs_result.scalars.return_value.all.return_value = []
    db.execute.return_value = pairs_result

    # source=1 excluded from pool, so no available candidates
    result = await pipeline.select_cross_pair(1, db, [1])
    assert result is None


# ── schedule_cross_message ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_schedule_cross_message_adds_pair_and_task(pipeline):
    db = AsyncMock()

    await pipeline.schedule_cross_message(1, 2, db)

    assert db.add.call_count == 2
    assert db.commit.await_count == 2
