import random
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.warmup import WarmupPipeline


def test_warmup_schedule_tier_progression_reactions_7days():
    pipeline = WarmupPipeline()

    class MockAccount:
        use_case = "reactions"
        warmup_tier = "fresh"
        warmup_day = 0

    actions = pipeline.get_allowed_actions(MockAccount())
    assert "profile_setup" in actions


def test_warmup_schedule_tier_progression_cold_dm_30days():
    pipeline = WarmupPipeline()

    class MockAccount:
        use_case = "cold_dm"
        warmup_tier = "fresh"
        warmup_day = 0

    actions = pipeline.get_allowed_actions(MockAccount())
    assert "profile_setup" in actions


def test_warmup_schedule_advance_tier_fires_webhook():
    pipeline = WarmupPipeline()

    class MockAccount:
        id = 1
        use_case = "reactions"
        warmup_tier = "basic"
        warmup_day = 14

    with patch("app.core.config.get_settings") as mock_settings:
        settings = MagicMock()
        settings.N8N_SYSTEM_WEBHOOK_URL = "http://test"
        mock_settings.return_value = settings

        with patch("app.services.webhook_sender.WebhookSender") as MockSender:
            instance = AsyncMock()
            instance.send = AsyncMock(return_value=True)
            MockSender.return_value = instance

            import asyncio

            tier = asyncio.run(
                pipeline.advance_tier_if_due(MockAccount(), AsyncMock(), AsyncMock())
            )

            assert tier == "intermediate"
            assert instance.send.called


def test_warmup_schedule_ready_transitions_account_to_active():
    pipeline = WarmupPipeline()

    class MockAccount:
        use_case = "reactions"
        warmup_tier = "intermediate"
        warmup_day = 14

    actions = pipeline.get_allowed_actions(MockAccount())
    assert actions


@pytest.mark.asyncio
async def test_warmup_anti_ring_bfs_detects_cycle():
    pipeline = WarmupPipeline()

    mock_db = AsyncMock()

    class MockResult:
        def all(self):
            return [(1,)]  # node 2 has outgoing edge to 1 (= source)

    mock_db.execute = AsyncMock(return_value=MockResult())

    assert await pipeline._would_form_ring(1, 2, mock_db) is True


@pytest.mark.asyncio
async def test_warmup_anti_ring_bfs_allows_tree():
    pipeline = WarmupPipeline()

    mock_db = AsyncMock()

    class MockResult:
        def all(self):
            return []

    mock_db.execute = AsyncMock(return_value=MockResult())

    assert await pipeline._would_form_ring(1, 2, mock_db) is False


@pytest.mark.asyncio
async def test_warmup_pair_cooldown_enforced():
    pipeline = WarmupPipeline()

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(
        return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )
    )

    target_pool = [2, 3, 4]

    with patch.object(pipeline, "_would_form_ring", return_value=False):
        candidate = await pipeline.select_cross_pair(1, mock_db, target_pool)

        assert candidate is not None


def test_warmup_random_delay_within_30_120s():
    random.seed(42)

    delays = []
    for _ in range(100):
        delay = random.randint(30, 120)
        delays.append(delay)

    assert all(30 <= d <= 120 for d in delays)
    assert max(delays) <= 120
    assert min(delays) >= 30
