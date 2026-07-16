"""Unit tests for WatcherRotationManager.rotate() and supporting methods."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.watchers.rotation_manager import WatcherRotationManager


@pytest.fixture
def manager():
    return WatcherRotationManager(process_id=42)


@pytest.mark.asyncio
async def test_rotate_updates_shard_from_db(manager):
    fake_accounts = [MagicMock(id=10), MagicMock(id=20), MagicMock(id=30)]

    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = fake_accounts
    db.execute.return_value = result

    mock_redis = AsyncMock()

    with patch("app.watchers.rotation_manager.get_redis", return_value=mock_redis):
        with patch("app.watchers.rotation_manager.watcher_shard_set", new_callable=AsyncMock) as mock_set:
            await manager.rotate(db)

    assert manager.current_shard == [10, 20, 30]
    assert mock_set.call_count == 2  # once to clear, once to set new shard


@pytest.mark.asyncio
async def test_heartbeat_writes_current_shard(manager):
    manager.current_shard = [1, 2, 3]
    mock_redis = AsyncMock()

    with patch("app.watchers.rotation_manager.get_redis", return_value=mock_redis):
        with patch("app.watchers.rotation_manager.watcher_shard_set", new_callable=AsyncMock) as mock_set:
            await manager.heartbeat(None)

    mock_set.assert_awaited_once_with(mock_redis, 42, [1, 2, 3])


@pytest.mark.asyncio
async def test_crash_recovery_claims_dead_watcher_accounts(manager):
    manager.current_shard = []

    mock_redis = AsyncMock()
    shards = {99: [5, 6, 7]}  # pid 99 is dead

    accounts = [MagicMock(id=5), MagicMock(id=6), MagicMock(id=7)]
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = accounts
    db.execute.return_value = result

    with patch("app.watchers.rotation_manager.get_redis", return_value=mock_redis):
        with patch("app.watchers.rotation_manager.watcher_shard_list_all", return_value=shards):
            with patch("app.watchers.rotation_manager.watcher_shard_set", new_callable=AsyncMock):
                with patch("psutil.pid_exists", return_value=False):
                    await manager.crash_recovery(db)

    assert manager.current_shard == [5, 6, 7]
