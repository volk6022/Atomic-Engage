import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.peer_resolver import PeerResolver


@pytest.fixture
def peer_resolver():
    return PeerResolver()


@pytest.mark.asyncio
async def test_peer_resolver_round_robin_distribution(peer_resolver):
    mock_redis = AsyncMock()

    call_count = 0

    async def mock_rate_increment(key, ttl):
        nonlocal call_count
        call_count += 1
        return call_count

    mock_redis.redis_helpers.rate_limit_increment = AsyncMock(
        side_effect=mock_rate_increment
    )
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.setex = AsyncMock()

    with patch.object(
        peer_resolver, "_check_rate_limit", new_callable=AsyncMock
    ) as mock_check:
        mock_check.side_effect = [False, True, False]

        result = await peer_resolver.select_account_for_resolution(
            [1, 2, 3], mock_redis
        )

        assert result == 2
        assert mock_check.call_count == 2


@pytest.mark.asyncio
async def test_peer_resolver_skips_account_at_rate_limit(peer_resolver):
    mock_redis = AsyncMock()

    call_count = 0

    async def mock_rate_increment(key, ttl):
        nonlocal call_count
        call_count += 1
        return 51

    mock_redis.rate_limit_increment = AsyncMock(side_effect=mock_rate_increment)
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.setex = AsyncMock()

    with patch.object(
        peer_resolver, "_check_rate_limit", new_callable=AsyncMock
    ) as mock_check:
        mock_check.return_value = False

        result = await peer_resolver.select_account_for_resolution(
            [1, 2, 3], mock_redis
        )

        assert result is None


@pytest.mark.asyncio
async def test_peer_resolver_returns_cached_peer_id_from_redis(peer_resolver):
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value="123456789")

    peer_id = await peer_resolver.resolve("testuser", MagicMock(), mock_redis)
    assert peer_id == 123456789


@pytest.mark.asyncio
async def test_peer_resolver_enqueues_resolve_when_cache_miss(peer_resolver):
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    mock_db = MagicMock()

    with patch.object(
        peer_resolver, "select_account_for_resolution", new_callable=AsyncMock
    ) as mock_select:
        mock_select.return_value = 1

        await peer_resolver.resolve("testuser", mock_db, mock_redis)
        assert mock_select.called
