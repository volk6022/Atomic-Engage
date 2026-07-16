import pytest
import respx
import httpx
from unittest.mock import AsyncMock, patch

from app.services.webhook_sender import WebhookSender


@pytest.fixture
def webhook_sender():
    return WebhookSender()


@pytest.mark.asyncio
async def test_webhook_sender_success_on_first_attempt(webhook_sender):
    with respx.mock() as mock:
        route = mock.post("https://example.com/webhook").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )

        result = await webhook_sender.send(
            1, "https://example.com/webhook", {"event": "test"}
        )

    assert result is True
    assert route.called
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_webhook_sender_retries_on_non2xx(webhook_sender):
    with respx.mock() as mock:
        route = mock.post("https://example.com/webhook").mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(200, json={"status": "ok"}),
            ]
        )

        with patch.object(webhook_sender, "_sleep", new_callable=AsyncMock):
            result = await webhook_sender.send(
                1, "https://example.com/webhook", {"event": "test"}
            )

    assert result is True
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_webhook_sender_exponential_backoff_schedule(webhook_sender):
    with respx.mock() as mock:
        route = mock.post("https://example.com/webhook").mock(
            side_effect=[httpx.Response(500), httpx.Response(500), httpx.Response(200)]
        )

        sleep_times = []

        async def mock_sleep(seconds):
            sleep_times.append(seconds)

        webhook_sender._sleep = mock_sleep
        result = await webhook_sender.send(
            1, "https://example.com/webhook", {"event": "test"}
        )

    assert result is True
    assert route.call_count == 3
    assert sleep_times == [30, 60]


@pytest.mark.asyncio
async def test_webhook_sender_marks_failed_after_5_attempts(webhook_sender):
    with respx.mock() as mock:
        route = mock.post("https://example.com/webhook").mock(
            return_value=httpx.Response(500)
        )

        with patch.object(webhook_sender, "_sleep", new_callable=AsyncMock):
            result = await webhook_sender.send(
                1, "https://example.com/webhook", {"event": "test"}
            )

    assert result is False
    assert route.call_count == 5


@pytest.mark.asyncio
async def test_webhook_sender_logs_payload_on_exhaustion(webhook_sender, caplog):
    with respx.mock() as mock:
        mock.post("https://example.com/webhook").mock(
            return_value=httpx.Response(500)
        )

        with patch.object(webhook_sender, "_sleep", new_callable=AsyncMock):
            result = await webhook_sender.send(
                1, "https://example.com/webhook", {"event": "test", "data": "value"}
            )

    assert result is False
    assert "exhausted_after_5_attempts" in caplog.text
    assert '{"event": "test"' in caplog.text
