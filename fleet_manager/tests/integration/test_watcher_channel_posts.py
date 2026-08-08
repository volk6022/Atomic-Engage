"""
Feature 005 — Watcher channel-post monitoring & enriched webhook payload.
Acceptance scenarios S1.SC1, S2.SC1, S3.SC1 (spec.md).

Real PG + Redis; only the outbound webhook is mocked (respx), per Constitution VII.
The handler receives the `session_maker` (production contract) and owns its session.
"""
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx as respx_lib

from app.watchers.update_handler import UpdateHandler

N8N_WEBHOOK_URL = "https://your-n8n-instance.com/webhook/fleet"


def _channel_message(chat_id, username, title, msg_id, text):
    chat = SimpleNamespace(
        id=chat_id,
        type=SimpleNamespace(value="channel"),
        username=username,
        title=title,
    )
    return SimpleNamespace(
        from_user=None,
        sender_chat=chat,
        chat=chat,
        id=msg_id,
        text=text,
        caption=None,
        date=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_s1_sc1_channel_post_webhook_within_5s(
    account_factory, session_maker, redis_client
):
    """A channel broadcast post is forwarded to n8n within 5s, flagged + enriched."""
    ids = await account_factory(status="active")
    account_id = ids["account_id"]

    msg = _channel_message(
        -1001627075588, "ru_pythonjobs", "Python Jobs", 4242,
        "Backend Python вакансия, удалёнка",
    )
    mock_client = AsyncMock()
    mock_client.resolve_peer = AsyncMock(return_value=MagicMock())

    handler = UpdateHandler()
    start = time.monotonic()
    with respx_lib.mock(assert_all_mocked=False) as mock:
        route = mock.post(N8N_WEBHOOK_URL).mock(return_value=httpx.Response(200))
        await handler.handle_new_message(
            client=mock_client,
            account_id=account_id,
            message=msg,
            db=session_maker,
            redis_conn=redis_client,
            webhook_url=N8N_WEBHOOK_URL,
        )
    elapsed = time.monotonic() - start

    assert route.called, "channel post must fire a webhook"
    assert elapsed < 5.0
    body = json.loads(route.calls[0].request.content)
    assert body["event"] == "incoming_message"
    assert body["is_channel_post"] is True
    assert body["chat_username"] == "ru_pythonjobs"
    assert body["chat_title"] == "Python Jobs"
    assert body["message"] == "Backend Python вакансия, удалёнка"
    assert body["message_id"] == 4242


@pytest.mark.asyncio
async def test_s2_sc1_payload_has_enriched_keys(
    account_factory, session_maker, redis_client
):
    """Every webhook body carries the enriched + legacy keys."""
    ids = await account_factory(status="active")
    account_id = ids["account_id"]
    msg = _channel_message(-100123, "datasciencejobs", "DS Jobs", 11, "ML role")

    mock_client = AsyncMock()
    mock_client.resolve_peer = AsyncMock(return_value=MagicMock())

    with respx_lib.mock(assert_all_mocked=False) as mock:
        route = mock.post(N8N_WEBHOOK_URL).mock(return_value=httpx.Response(200))
        await UpdateHandler().handle_new_message(
            client=mock_client, account_id=account_id, message=msg,
            db=session_maker, redis_conn=redis_client, webhook_url=N8N_WEBHOOK_URL,
        )

    body = json.loads(route.calls[0].request.content)
    for key in (
        "event", "account_id", "from_peer_id", "chat_id", "message", "message_id",
        "date", "chat_username", "chat_title", "chat_type", "is_channel_post",
        "sender_username",
    ):
        assert key in body, f"missing key {key!r} in webhook payload"


@pytest.mark.asyncio
async def test_s3_sc1_duplicate_message_suppressed(
    account_factory, session_maker, redis_client
):
    """Re-handling the same (chat_id, message_id) fires the webhook only once."""
    ids = await account_factory(status="active")
    account_id = ids["account_id"]
    msg = _channel_message(-100555, "remote_ai_jobs", "Remote AI", 77, "AI Engineer")

    mock_client = AsyncMock()
    mock_client.resolve_peer = AsyncMock(return_value=MagicMock())

    with respx_lib.mock(assert_all_mocked=False) as mock:
        route = mock.post(N8N_WEBHOOK_URL).mock(return_value=httpx.Response(200))
        for _ in range(2):
            await UpdateHandler().handle_new_message(
                client=mock_client, account_id=account_id, message=msg,
                db=session_maker, redis_conn=redis_client, webhook_url=N8N_WEBHOOK_URL,
            )

    assert route.call_count == 1, "duplicate message must not be forwarded twice"
