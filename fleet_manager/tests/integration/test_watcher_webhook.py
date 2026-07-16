"""
Story 2: Real-Time Incoming Message Monitoring
Acceptance scenarios from spec.md §User Story 2
"""
import json
import time
import pytest
import httpx
import respx as respx_lib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select

from app.db.models import Account
from app.watchers.update_handler import UpdateHandler
from app.watchers.rotation_manager import WatcherRotationManager

N8N_WEBHOOK_URL = "https://your-n8n-instance.com/webhook/fleet"


@pytest.mark.asyncio
async def test_s2_sc1_incoming_message_webhook_within_5s(account_factory, session_maker, redis_client):
    """
    Given an active account enrolled in watcher rotation,
    When that account receives a Telegram direct message,
    Then n8n receives a webhook within 5 seconds containing full message context.
    """
    ids = await account_factory(status="active")
    account_id = ids["account_id"]

    fake_user = MagicMock()
    fake_user.id = 555000111
    fake_user.username = "test_sender"
    fake_user.first_name = "Test"
    fake_user.last_name = "Sender"

    fake_message = MagicMock()
    fake_message.from_user = fake_user
    fake_message.chat = MagicMock()
    fake_message.chat.id = 555000111
    fake_message.id = 9001
    fake_message.text = "Hello fleet!"
    fake_message.date = datetime.now(timezone.utc)

    mock_client = AsyncMock()
    mock_client.resolve_peer = AsyncMock(return_value=MagicMock())

    handler = UpdateHandler()
    start = time.monotonic()

    with respx_lib.mock(assert_all_mocked=False) as mock:
        webhook_route = mock.post(N8N_WEBHOOK_URL).mock(return_value=httpx.Response(200))

        async with session_maker() as session:
            await handler.handle_new_message(
                client=mock_client,
                account_id=account_id,
                message=fake_message,
                db=session,
                redis_conn=redis_client,
                webhook_url=N8N_WEBHOOK_URL,
            )

    elapsed = time.monotonic() - start

    assert webhook_route.called, "Webhook must be fired when message arrives"
    assert elapsed < 5.0, f"Webhook fired in {elapsed:.2f}s — must be under 5s"

    body = json.loads(webhook_route.calls[0].request.content)
    assert body["event"] == "incoming_message"
    assert body["account_id"] == account_id
    assert body["from_peer_id"] == fake_user.id
    assert body["message"] == fake_message.text


@pytest.mark.asyncio
async def test_s2_sc2_rotation_cycle_accounts(account_factory, session_maker, redis_client):
    """
    Given the fleet has active accounts,
    When WatcherRotationManager claims a shard,
    Then at most SHARD_SIZE (75) accounts are claimed per process instance,
    ensuring fair distribution over the rotation cycle.
    """
    for _ in range(10):
        await account_factory(status="active")

    manager = WatcherRotationManager(process_id=99901)

    async with session_maker() as session:
        shard = await manager.on_startup(session)

    assert isinstance(shard, list)
    assert len(shard) <= WatcherRotationManager.SHARD_SIZE, (
        f"Shard size {len(shard)} must not exceed {WatcherRotationManager.SHARD_SIZE}"
    )
    assert len(shard) > 0, "Shard must contain at least one active account"

    if redis_client:
        from app.db.redis_client import watcher_shard_list_all
        shards = await watcher_shard_list_all(redis_client)
        found = any(
            str(99901) == str(k) for k in shards.keys()
        )
        assert found, "Shard must be registered in Redis"

    if manager.rotation_task:
        manager.rotation_task.cancel()
        try:
            await manager.rotation_task
        except Exception:
            pass


def test_s2_sc3_memory_threshold_graceful_restart():
    """
    Given a watcher process monitors memory usage,
    When host memory exceeds the restart threshold (85%),
    Then `memory_over_threshold()` reports True so the monitor loop will sys.exit(1)
    and Docker restarts the process, which reclaims its shard on startup.

    This drives the REAL production predicate (app.watchers.watcher_process), mocking
    only psutil — not a re-implementation of the check inside the test.
    """
    from app.watchers.watcher_process import (
        MEMORY_RESTART_THRESHOLD,
        memory_over_threshold,
    )

    high = MagicMock(percent=90.0)
    low = MagicMock(percent=42.0)
    edge = MagicMock(percent=MEMORY_RESTART_THRESHOLD)  # exactly at the line → not over

    with patch("psutil.virtual_memory", return_value=high):
        assert memory_over_threshold() is True
    with patch("psutil.virtual_memory", return_value=low):
        assert memory_over_threshold() is False
    with patch("psutil.virtual_memory", return_value=edge):
        assert memory_over_threshold() is False
