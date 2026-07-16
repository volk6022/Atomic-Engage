"""Unit tests for fleet API endpoints called directly (bypasses ASGI routing)."""
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_get_fleet_status_returns_account_counts():
    from app.api.v1.fleet import get_fleet_status

    rows = [("active", 3), ("sleeping", 1), ("banned", 2)]
    result = MagicMock()
    result.all.return_value = rows

    db = AsyncMock()
    db.execute.return_value = result

    response = await get_fleet_status(db=db, api_key="key")
    assert response == {"accounts": {"active": 3, "sleeping": 1, "banned": 2}}


@pytest.mark.asyncio
async def test_get_fleet_status_empty_fleet():
    from app.api.v1.fleet import get_fleet_status

    result = MagicMock()
    result.all.return_value = []

    db = AsyncMock()
    db.execute.return_value = result

    response = await get_fleet_status(db=db, api_key="key")
    assert response == {"accounts": {}}


@pytest.mark.asyncio
async def test_health_check_returns_ok():
    from app.api.v1.fleet import health_check

    response = await health_check()
    assert response == {"status": "ok"}
