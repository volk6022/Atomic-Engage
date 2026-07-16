"""
Story 1: Dispatch Telegram Actions via n8n
Acceptance scenarios from spec.md §User Story 1
"""
import json
import pytest
import httpx
import respx as respx_lib
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from unittest.mock import MagicMock

from app.db.models import Account, Task
from app.core.constants import AccountStatus, TaskStatus

API_KEY = "change_me_in_production"
N8N_WEBHOOK_URL = "https://your-n8n-instance.com/webhook/fleet"


@pytest.mark.asyncio
async def test_s1_sc1_send_message_success(async_client, account_factory, session_maker):
    """
    Given an active account with matching geo-proxy and completed warmup,
    When n8n submits a send-message action,
    Then the system returns 202 Accepted with a Task ID and the task is stored as queued.
    """
    ids = await account_factory(
        phone_country="RU",
        proxy_country="RU",
        status="active",
        warmup_tier="ready",
    )
    account_id = ids["account_id"]

    with respx_lib.mock(assert_all_mocked=False):
        response = await async_client.post(
            "/v1/action",
            json={
                "account_id": account_id,
                "action": "send_message",
                "payload": {"peer_id": 987654321, "text": "Hello from test"},
                "webhook_url": "https://n8n.example.com/webhook/result",
                "priority": 5,
            },
            headers={"X-API-Key": API_KEY},
        )

    assert response.status_code == 202, response.text
    data = response.json()
    assert "task_id" in data
    assert data["status"] == "queued"
    assert data["account_id"] == account_id

    async with session_maker() as session:
        result = await session.execute(
            select(Task).where(Task.external_id == data["task_id"])
        )
        task = result.scalar_one_or_none()

    assert task is not None
    assert task.status == TaskStatus.QUEUED
    assert task.task_type == "send_message"
    assert task.account_id == account_id


@pytest.mark.asyncio
async def test_s1_sc2_geo_mismatch_reject(async_client, account_factory, session_maker):
    """
    Given an account whose phone country does not match its proxy country,
    When n8n submits any action for that account,
    Then the system rejects with 409, transitions account to sleeping, and fires alert webhook.
    """
    ids = await account_factory(
        phone_country="RU",
        proxy_country="US",  # mismatch → CRITICAL
        status="active",
    )
    account_id = ids["account_id"]

    with respx_lib.mock(assert_all_mocked=False) as mock:
        webhook_route = mock.post(N8N_WEBHOOK_URL).mock(
            return_value=httpx.Response(200)
        )

        response = await async_client.post(
            "/v1/action",
            json={
                "account_id": account_id,
                "action": "send_message",
                "payload": {"peer_id": 111, "text": "should be rejected"},
                "webhook_url": "https://n8n.example.com/webhook/result",
            },
            headers={"X-API-Key": API_KEY},
        )

    assert response.status_code == 409, response.text
    assert "Geographic mismatch" in response.json()["detail"]

    async with session_maker() as session:
        result = await session.execute(
            select(Account).where(Account.id == account_id)
        )
        account = result.scalar_one_or_none()

    assert account.status == AccountStatus.SLEEPING

    assert webhook_route.called, "geo_reject webhook must be sent to N8N_SYSTEM_WEBHOOK_URL"
    body = json.loads(webhook_route.calls[0].request.content)
    assert body["event"] == "geo_reject"
    assert body["account_id"] == account_id


@pytest.mark.asyncio
async def test_s1_sc3_username_resolution_round_robin(
    async_client, account_factory, session_maker
):
    """
    Given a target username that has not been previously resolved,
    When n8n submits a resolve_username action,
    Then the system accepts it (202) and queues a resolve_username task for the
    account (the round-robin-within-daily-limits selection itself is unit-covered
    in test_peer_resolver.py). FR-009 / Story 1 scenario 3.
    """
    ids = await account_factory(
        phone_country="RU", proxy_country="RU", status="active", warmup_tier="ready"
    )
    account_id = ids["account_id"]

    with respx_lib.mock(assert_all_mocked=False):
        response = await async_client.post(
            "/v1/action",
            json={
                "account_id": account_id,
                "action": "resolve_username",
                "payload": {"username": "some_unresolved_user"},
                "webhook_url": "https://n8n.example.com/webhook/result",
            },
            headers={"X-API-Key": API_KEY},
        )

    assert response.status_code == 202, response.text
    data = response.json()
    assert data["status"] == "queued"

    async with session_maker() as session:
        result = await session.execute(
            select(Task).where(Task.external_id == data["task_id"])
        )
        task = result.scalar_one_or_none()

    assert task is not None
    assert task.task_type == "resolve_username"
    assert task.status == TaskStatus.QUEUED
    assert task.account_id == account_id


@pytest.mark.asyncio
async def test_s1_sc4_flood_wait_handling(account_factory, session_maker):
    """
    Given an account whose flood_until is in the future (FloodWait already received),
    When BaseTask.prepare() is called for that account,
    Then it returns None (deferred) — no task executes until the flood period expires.
    """
    from app.workers.base_task import BaseTask

    flood_until = datetime.now(timezone.utc) + timedelta(hours=2)
    ids = await account_factory(
        status="active",
        flood_until=flood_until,
    )
    account_id = ids["account_id"]

    ctx = MagicMock()

    async with session_maker() as session:
        result = await BaseTask.prepare(ctx, account_id, session)

    assert result is None, (
        "BaseTask.prepare() must return None when account.flood_until is in the future"
    )
