"""Unit tests for ProxyManager covering all public methods."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.proxy_manager import ProxyManager


@pytest.fixture
def manager():
    return ProxyManager()


# ── parse_proxy_url ──────────────────────────────────────────────────────────

def test_parse_proxy_url_full(manager):
    host, port, user, pw = manager.parse_proxy_url(
        "http://myuser:mypass@192.168.1.1:3128"
    )
    assert host == "192.168.1.1"
    assert port == 3128
    assert user == "myuser"
    assert pw == "mypass"


def test_parse_proxy_url_no_auth(manager):
    # No credentials: host/port parsed, user/pass None (urlparse-based).
    host, port, user, pw = manager.parse_proxy_url("http://proxy.example.com:8080/path")
    assert host == "proxy.example.com"
    assert port == 8080
    assert user is None and pw is None


def test_parse_proxy_url_socks5_with_underscore_login(manager):
    # socks5 scheme + provider login encoding the exit country (proxy style).
    host, port, user, pw = manager.parse_proxy_url(
        "socks5://abc__cr.us:secret@proxy-host.example.com:11000"
    )
    assert host == "proxy-host.example.com" and port == 11000
    assert user == "abc__cr.us" and pw == "secret"


def test_parse_proxy_url_username_only(manager):
    host, port, user, pw = manager.parse_proxy_url(
        "http://onlyuser@10.0.0.1:1080"
    )
    assert user == "onlyuser"
    assert pw is None


def test_parse_proxy_url_no_port(manager):
    host, port, user, pw = manager.parse_proxy_url(
        "http://myuser:mypass@192.168.1.1"
    )
    assert port == 8080


def test_parse_proxy_url_malformed_returns_defaults(manager):
    host, port, user, pw = manager.parse_proxy_url("not-a-valid-url")
    assert port == 8080
    assert user is None


# ── geo_validate ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_geo_validate_calls_validator(manager):
    with patch.object(manager.validator, "get_proxy_info", return_value=("RU", "ISP", 10800)):
        with patch.object(manager.validator, "validate") as mock_validate:
            mock_validate.return_value = MagicMock(risk="OK")
            result, country = await manager.geo_validate(
                "http://u:p@10.0.0.1:1080", "RU", None
            )
    assert country == "RU"
    mock_validate.assert_called_once_with(
        phone_country="RU", proxy_country="RU", asn_org="ISP"
    )


# ── health_check ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_check_returns_true_on_success(manager):
    writer = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()

    with patch("asyncio.open_connection", return_value=(AsyncMock(), writer)):
        with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = (AsyncMock(), writer)
            result = await manager.health_check("http://u:p@10.0.0.1:1080", timeout=5.0)

    assert result is True


@pytest.mark.asyncio
async def test_health_check_returns_false_on_timeout(manager):
    import asyncio

    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
        result = await manager.health_check("http://u:p@10.0.0.1:1080", timeout=1.0)

    assert result is False


@pytest.mark.asyncio
async def test_health_check_returns_false_on_connection_error(manager):
    with patch("asyncio.wait_for", side_effect=ConnectionRefusedError()):
        result = await manager.health_check("http://u:p@10.0.0.1:1080")

    assert result is False


# ── assign_reserve ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_assign_reserve_returns_none_when_no_proxy(manager):
    account = MagicMock()
    account.phone_country = "RU"

    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result

    redis_conn = AsyncMock()
    reserve = await manager.assign_reserve(account, db, redis_conn)
    assert reserve is None


@pytest.mark.asyncio
async def test_assign_reserve_assigns_found_proxy(manager):
    account = MagicMock()
    account.phone_country = "RU"

    proxy = MagicMock()
    proxy.state = "reserve"

    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = proxy
    db.execute.return_value = result

    redis_conn = AsyncMock()
    reserve = await manager.assign_reserve(account, db, redis_conn)

    assert reserve is proxy
    assert proxy.state == "assigned"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_assign_reserve_returns_none_if_account_missing_phone_country(manager):
    account = object()  # no phone_country attribute
    db = AsyncMock()
    redis_conn = AsyncMock()
    result = await manager.assign_reserve(account, db, redis_conn)
    assert result is None
