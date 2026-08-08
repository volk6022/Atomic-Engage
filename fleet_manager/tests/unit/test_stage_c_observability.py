"""Task read API, non-blocking webhook delivery, and watcher payload enrichment."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.api.v1.tasks import _summary
from app.db.models import WebhookDelivery
from app.watchers._message_payload import build_incoming_message_payload
from app.workers.deliver_webhook import _backoff_seconds, deliver_webhook


def _task(**kw):
    base = dict(
        external_id="ext-1", account_id=7, task_type="get_chat_history",
        status="complete", error_code=None, priority=5, retry_count=0,
        result={"posts": ["x"] * 1000},
        created_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        started_at=None, updated_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── task read API ──────────────────────────────────────────────────────────────
def test_summary_reports_external_id_as_task_id():
    """Callers only ever hold the external UUID; the autoincrement id is not API."""
    assert _summary(_task())["task_id"] == "ext-1"


def test_summary_omits_result():
    """A 1000-message history is hundreds of KB; listing must not carry it."""
    assert "result" not in _summary(_task())


# ── webhook delivery ───────────────────────────────────────────────────────────
def test_backoff_saturates_at_the_last_step():
    assert _backoff_seconds(1) == 30
    assert _backoff_seconds(2) == 60
    # Beyond the configured schedule the delay must not index out of range.
    assert _backoff_seconds(99) == 480


class _FakeSessionMaker:
    def __init__(self, delivery):
        self.delivery = delivery

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, model, pk):
        return self.delivery

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_delivery_success_marks_row_delivered():
    delivery = WebhookDelivery(url="http://x/y", payload={}, status="pending", attempts=0)
    ctx = {"session_maker": _FakeSessionMaker(delivery), "redis": AsyncMock()}

    with patch("app.workers.deliver_webhook.WebhookSender") as sender:
        sender.return_value.send_once = AsyncMock(return_value=(True, "200"))
        out = await deliver_webhook(ctx, 1)

    assert out["delivered"] is True
    assert delivery.status == "delivered"
    assert delivery.attempts == 1
    assert delivery.delivered_at is not None


@pytest.mark.asyncio
async def test_delivery_failure_reschedules_without_sleeping():
    delivery = WebhookDelivery(url="http://x/y", payload={}, status="pending", attempts=0)
    redis = AsyncMock()
    ctx = {"session_maker": _FakeSessionMaker(delivery), "redis": redis}

    with patch("app.workers.deliver_webhook.WebhookSender") as sender:
        sender.return_value.send_once = AsyncMock(return_value=(False, "ConnectError"))
        out = await deliver_webhook(ctx, 1)

    assert out["delivered"] is False
    assert delivery.status == "pending"
    assert delivery.next_attempt_at is not None
    redis.enqueue_job.assert_awaited_once()
    assert redis.enqueue_job.await_args.kwargs["_defer_by"] == timedelta(seconds=30)


@pytest.mark.asyncio
async def test_delivery_gives_up_after_max_attempts():
    delivery = WebhookDelivery(url="http://x/y", payload={}, status="pending", attempts=4)
    redis = AsyncMock()
    ctx = {"session_maker": _FakeSessionMaker(delivery), "redis": redis}

    with patch("app.workers.deliver_webhook.WebhookSender") as sender:
        sender.return_value.send_once = AsyncMock(return_value=(False, "HTTP 500"))
        out = await deliver_webhook(ctx, 1)

    assert out["exhausted"] is True
    assert delivery.status == "failed"
    redis.enqueue_job.assert_not_awaited()


# ── watcher payload ────────────────────────────────────────────────────────────
def _msg(**kw):
    base = dict(
        id=101, text="hi", date=None, from_user=None, sender_chat=None,
        chat=SimpleNamespace(id=-100, username="disc", title="D", type="supergroup"),
        reply_to_message_id=None, message_thread_id=None,
        reply_to_top_message_id=None, automatic_forward=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_payload_carries_author_and_thread():
    user = SimpleNamespace(id=5, username="petr", first_name="Пётр",
                           last_name="И", is_bot=False)
    p = build_incoming_message_payload(1, _msg(from_user=user,
                                               reply_to_message_id=90,
                                               reply_to_top_message_id=50))
    assert p["from_first_name"] == "Пётр"
    assert p["from_is_bot"] is False
    assert p["reply_to_message_id"] == 90
    assert p["message_thread_id"] == 50


def test_payload_survives_a_channel_post():
    """A linked channel's post arrives with from_user=None; it must not raise."""
    p = build_incoming_message_payload(1, _msg(
        sender_chat=SimpleNamespace(id=-200, username="chan"),
        automatic_forward=True))
    assert p["from_first_name"] is None
    assert p["sender_chat_id"] == -200
    assert p["is_automatic_forward"] is True


def test_payload_reads_the_unprefixed_forward_attr():
    """kurigram spells it `automatic_forward`; reading `is_automatic_forward` off the
    message would yield False forever and erase the comment/post distinction."""
    msg = _msg()
    del msg.automatic_forward
    msg.is_automatic_forward = True          # the name that does NOT exist upstream
    assert build_incoming_message_payload(1, msg)["is_automatic_forward"] is False
