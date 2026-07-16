"""Unit tests for create_action endpoint called directly (bypasses ASGI)."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.v1.actions import ActionRequest, create_action
from app.core.constants import AccountStatus, TaskStatus


def _make_request(**kwargs):
    defaults = {
        "account_id": 1,
        "action": "send_message",
        "payload": {"peer_id": 123, "text": "hi"},
        "webhook_url": "https://n8n.example.com/webhook/result",
        "priority": 5,
    }
    defaults.update(kwargs)
    return ActionRequest(**defaults)


def _make_account(
    status="active",
    phone_country="RU",
    proxy_country="RU",
    use_case="reactions",
    warmup_tier="ready",
):
    proxy = MagicMock()
    proxy.country = proxy_country

    account = MagicMock()
    account.id = 1
    account.status = status
    account.phone_country = phone_country
    account.proxy = proxy
    # warmup gate reads these; a `ready` reactions account may send_message
    account.use_case = use_case
    account.warmup_tier = warmup_tier
    return account


@pytest.mark.asyncio
async def test_create_action_invalid_action_raises_422():
    from fastapi import HTTPException

    db = AsyncMock()
    request = _make_request(action="invalid_action")

    with pytest.raises(HTTPException) as exc_info:
        await create_action(request=request, db=db, api_key="key")

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_create_action_account_not_found_raises_404():
    from fastapi import HTTPException

    result = MagicMock()
    result.scalar_one_or_none.return_value = None

    db = AsyncMock()
    db.execute.return_value = result

    with pytest.raises(HTTPException) as exc_info:
        await create_action(request=_make_request(), db=db, api_key="key")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_create_action_banned_account_raises_409():
    from fastapi import HTTPException

    account = _make_account(status=AccountStatus.BANNED)
    result = MagicMock()
    result.scalar_one_or_none.return_value = account

    db = AsyncMock()
    db.execute.return_value = result

    with pytest.raises(HTTPException) as exc_info:
        await create_action(request=_make_request(), db=db, api_key="key")

    assert exc_info.value.status_code == 409
    assert "banned" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_create_action_geo_mismatch_raises_409():
    import respx, httpx
    from fastapi import HTTPException

    account = _make_account(phone_country="RU", proxy_country="US")
    result = MagicMock()
    result.scalar_one_or_none.return_value = account

    db = AsyncMock()
    db.execute.return_value = result

    with respx.mock(assert_all_mocked=False) as mock:
        mock.post("https://your-n8n-instance.com/webhook/fleet").mock(
            return_value=httpx.Response(200)
        )
        with pytest.raises(HTTPException) as exc_info:
            await create_action(request=_make_request(), db=db, api_key="key")

    assert exc_info.value.status_code == 409
    assert account.status == AccountStatus.SLEEPING


@pytest.mark.asyncio
async def test_create_action_success_returns_202_payload():
    account = _make_account()

    db_result = MagicMock()
    db_result.scalar_one_or_none.return_value = account

    task_mock = MagicMock()
    task_mock.id = 99

    db = AsyncMock()
    db.execute.return_value = db_result
    db.refresh = AsyncMock(side_effect=lambda t: None)

    mock_pool = AsyncMock()

    with patch("arq.create_pool", return_value=mock_pool):
        response = await create_action(request=_make_request(), db=db, api_key="key")

    assert response.status == "queued"
    assert response.account_id == 1
    assert response.task_id is not None
    mock_pool.enqueue_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_action_warmup_gate_blocks_unwarmed():
    """A fresh reactions account may not send_message yet (FR-110 / G4)."""
    from fastapi import HTTPException

    account = _make_account(use_case="reactions", warmup_tier="fresh")
    result = MagicMock()
    result.scalar_one_or_none.return_value = account
    db = AsyncMock()
    db.execute.return_value = result

    with pytest.raises(HTTPException) as exc_info:
        await create_action(
            request=_make_request(action="send_message"), db=db, api_key="key"
        )

    assert exc_info.value.status_code == 409
    assert "warmed" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_create_action_resolve_username_exempt_from_warmup():
    """resolve_username is an infra lookup — allowed even on a fresh account."""
    account = _make_account(use_case="reactions", warmup_tier="fresh")
    result = MagicMock()
    result.scalar_one_or_none.return_value = account
    db = AsyncMock()
    db.execute.return_value = result
    db.refresh = AsyncMock(side_effect=lambda t: None)

    with patch("arq.create_pool", return_value=AsyncMock()):
        response = await create_action(
            request=_make_request(action="resolve_username", payload={"username": "bob"}),
            db=db,
            api_key="key",
        )

    assert response.status == "queued"
