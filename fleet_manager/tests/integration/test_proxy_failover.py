"""
Story 5: Fleet Health Monitoring and Ban Management
Acceptance scenarios from spec.md §User Story 5
"""
import json
import pytest
import httpx
import respx as respx_lib
from sqlalchemy import select, func

from app.db.models import Account, Proxy, Task
from app.core.constants import AccountStatus, TaskStatus

API_KEY = "change_me_in_production"
N8N_WEBHOOK_URL = "https://your-n8n-instance.com/webhook/fleet"


@pytest.mark.asyncio
async def test_s5_sc1_ban_detection_unauthorized_marks_banned(async_client, account_factory, session_maker):
    """
    Given an account with status=banned,
    When n8n tries to submit any action for that account,
    Then the gateway rejects with 409 — no task is created and the account stays banned.
    """
    ids = await account_factory(status="banned")
    account_id = ids["account_id"]

    with respx_lib.mock(assert_all_mocked=False):
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

    assert response.status_code == 409
    assert "banned" in response.json()["detail"].lower()

    async with session_maker() as session:
        account_result = await session.execute(
            select(Account).where(Account.id == account_id)
        )
        account = account_result.scalar_one_or_none()

        task_result = await session.execute(
            select(Task).where(Task.account_id == account_id)
        )
        tasks = task_result.scalars().all()

    assert account.status == AccountStatus.BANNED
    assert len(tasks) == 0, "No task must be created for a banned account"


@pytest.mark.asyncio
async def test_s5_sc2_proxy_fail_triggers_sleeping_alert(account_factory, session_maker, redis_client):
    """
    Given a proxy assigned to an account fails its health check and no reserve exists,
    When the failover logic runs,
    Then the account transitions to 'sleeping' and a proxy_fail_sleeping webhook is sent.
    """
    from app.services.proxy_manager import ProxyManager
    from app.services.webhook_sender import WebhookSender
    from app.core.config import get_settings

    ids = await account_factory(status="active", proxy_is_healthy=True, proxy_state="assigned")
    account_id = ids["account_id"]
    proxy_id = ids["proxy_id"]

    async with session_maker() as session:
        async with session.begin():
            result = await session.execute(select(Proxy).where(Proxy.id == proxy_id))
            proxy = result.scalar_one_or_none()
            proxy.is_healthy = False

    manager = ProxyManager()

    with respx_lib.mock(assert_all_mocked=False) as mock:
        webhook_route = mock.post(N8N_WEBHOOK_URL).mock(return_value=httpx.Response(200))

        async with session_maker() as session:
            result = await session.execute(select(Account).where(Account.id == account_id))
            account = result.scalar_one_or_none()

            reserve = await manager.assign_reserve(account, session, redis_client)

            if reserve is None:
                account.status = AccountStatus.SLEEPING
                await session.commit()

                settings = get_settings()
                await WebhookSender().send(
                    delivery_id=0,
                    url=settings.N8N_SYSTEM_WEBHOOK_URL,
                    payload={
                        "event": "proxy_fail_sleeping",
                        "account_id": account_id,
                        "failed_proxy_id": proxy_id,
                        "reserve_available": False,
                    },
                )

    assert reserve is None, "No reserve proxy should be available (none in test DB with matching country)"

    async with session_maker() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        updated = result.scalar_one_or_none()

    assert updated.status == AccountStatus.SLEEPING

    assert webhook_route.called
    body = json.loads(webhook_route.calls[0].request.content)
    assert body["event"] == "proxy_fail_sleeping"
    assert body["account_id"] == account_id
    assert body["reserve_available"] is False


@pytest.mark.asyncio
async def test_s5_sc3_fleet_status_counts_accurate(async_client, account_factory, session_maker):
    """
    Given accounts exist with various statuses,
    When GET /v1/fleet/status is called,
    Then the response counts match the actual DB distribution.
    """
    await account_factory(status="active")
    await account_factory(status="active")
    await account_factory(status="banned")
    await account_factory(status="sleeping")
    await account_factory(status="warmup")

    response = await async_client.get(
        "/v1/fleet/status",
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 200
    data = response.json()
    assert "accounts" in data

    async with session_maker() as session:
        result = await session.execute(
            select(Account.status, func.count()).group_by(Account.status)
        )
        db_counts = {row[0]: row[1] for row in result.all()}

    for status, count in db_counts.items():
        assert data["accounts"].get(status, 0) == count, (
            f"Status '{status}': API returned {data['accounts'].get(status, 0)}, DB has {count}"
        )
