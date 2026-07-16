import pytest
from pydantic import ValidationError

from app.api.v1.webhook_events import (
    TaskCompleteEvent,
    TaskFailedEvent,
    BanAlertEvent,
    GeoRejectEvent,
    FloodWaitEvent,
    IncomingMessageEvent,
)


def test_task_complete_event_valid():
    event = TaskCompleteEvent(
        task_id="test-uuid", account_id=1, result={"telegram_message_id": 123456789}
    )
    assert event.result["telegram_message_id"] == 123456789


def test_task_complete_event_missing_result():
    with pytest.raises(ValidationError):
        TaskCompleteEvent(task_id="test-uuid", account_id=1)


def test_task_failed_event_valid():
    event = TaskFailedEvent(
        task_id="test-uuid", account_id=1, error_code="BAN_DETECTED", retry_count=0
    )
    assert event.error_code == "BAN_DETECTED"


def test_ban_alert_event_valid():
    event = BanAlertEvent(account_id=1, ban_reason="Unauthorized")
    assert event.ban_reason == "Unauthorized"


def test_geo_reject_event_valid():
    event = GeoRejectEvent(
        account_id=1, phone_country="RU", proxy_country="US", risk="CRITICAL"
    )
    assert event.risk == "CRITICAL"


def test_flood_wait_event_valid():
    event = FloodWaitEvent(
        account_id=1, flood_until="2026-05-04T12:00:00Z", retry_in=60
    )
    assert event.retry_in == 60


def test_incoming_message_event_valid():
    event = IncomingMessageEvent(
        account_id=1, from_peer_id=123456789, message="Hello", message_id=999
    )
    assert event.message == "Hello"
