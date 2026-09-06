"""Отравленный пул вебхуков и обещанный, но несуществующий пере-драйвер (план 3.2).

Две половины одного отказа, и обе тихие.

**Половина первая — пул переживает свою смерть.** `webhook_queue._get_pool()` кеширует
пул arq на процесс. `enqueue_webhook` ловит любое исключение постановки, пишет в лог и
**оставляет мёртвый объект в `_pool`**. То есть один обрыв Redis отравляет процесс
навсегда: каждый следующий вебхук падает на том же мёртвом пуле до перезапуска, а
процесс при этом здоров с виду. Та же форма, что авария 18.08.

**Половина вторая — «can be re-driven» некому исполнить.** Комментарий в
`enqueue_webhook` обещает, что строка останется `pending` и её можно перезапустить.
Перезапускать нечем: в `arq_settings.cron_jobs` было ровно три крона, и ни один не
смотрит в `webhook_deliveries`. Строка `pending` живёт вечно, а для Радара это лид,
который не приехал.

Сюда же попадает вторая дорога к тому же состоянию: `deliver_webhook` при отсутствии
пула (`ctx["redis"] is None`) честно пишет `webhook_retry_unschedulable` и оставляет
строку `pending` — тоже навсегда.

Проверяемый контракт:

    app/services/webhook_queue.py
        reset_pool() -> None
            — сбросить кеш; следующий вызов строит пул заново
        enqueue_webhook(...)
            — при отказе постановки сбрасывает пул, строка остаётся `pending`

    app/workers/webhook_redrive.py
        REDRIVE_AFTER_SECONDS: int
        async redrive_pending_webhooks(db, redis=None, limit=100, now=None) -> list[int]
            — возвращает id, которые ДЕЙСТВИТЕЛЬНО поставлены в очередь

    app/workers/arq_settings.py
        cron webhook_redrive_tick зарегистрирован, и его расписание ВЫЧИСЛЯЕТСЯ

⚠️ Окно `REDRIVE_AFTER_SECONDS` обязано быть больше последнего шага `WEBHOOK_BACKOFF`
(480 с), иначе пере-драйвер начнёт обгонять штатную отсрочку и слать один вебхук
дважды. Проверяется отдельно — это не деталь реализации, а условие безопасности.
"""
import pytest

pytest.importorskip("pyrogram")  # arq_settings -> воркеры -> _tg_errors -> pyrogram

from datetime import datetime, timedelta, timezone  # noqa: E402

from sqlalchemy import delete, select  # noqa: E402

from app.db.models import WebhookDelivery  # noqa: E402
from app.services import webhook_queue  # noqa: E402
from app.services.webhook_sender import MAX_ATTEMPTS, WEBHOOK_BACKOFF  # noqa: E402

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
URL = "http://radar.invalid/api/v1/ingest/tok?kind=history"


class _FakePool:
    """Пул, который умеет только считать постановки и падать по команде."""

    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args, **kw):
        if self.broken:
            raise ConnectionError("Redis ушёл")
        self.jobs.append((name, args, kw))
        return object()


@pytest.fixture()
def clean_deliveries(session_maker):
    """Своя песочница в общей таблице: тесты Engage делят одну базу."""
    async def _wipe():
        async with session_maker() as db:
            await db.execute(delete(WebhookDelivery).where(
                WebhookDelivery.url.like("http://radar.invalid/%")))
            await db.commit()
    return _wipe


async def _add(session_maker, **over) -> int:
    fields = dict(url=URL, payload={"event": "x"}, status="pending", attempts=0,
                  next_attempt_at=None)
    fields.update(over)
    created = fields.pop("created_at", None)
    async with session_maker() as db:
        row = WebhookDelivery(**fields)
        db.add(row)
        await db.flush()
        if created is not None:
            row.created_at = created
        await db.commit()
        return row.id


async def _state(session_maker, delivery_id: int) -> WebhookDelivery:
    async with session_maker() as db:
        return (await db.execute(select(WebhookDelivery).where(
            WebhookDelivery.id == delivery_id))).scalar_one()


# ── половина первая: пул не имеет права пережить свою смерть ──────────────────

@pytest.mark.asyncio
async def test_a_failed_enqueue_drops_the_cached_pool(session_maker, clean_deliveries,
                                                      monkeypatch):
    """Один обрыв Redis не должен отравлять процесс до перезапуска.

    Проверяется не «залогировали ли», а то, что СЛЕДУЮЩИЙ вебхук строит пул заново:
    именно этого не хватало, и именно поэтому отказ выглядел как здоровый процесс,
    переставший доставлять лиды.
    """
    await clean_deliveries()
    built: list[_FakePool] = []

    async def fake_create_pool(*a, **kw):
        pool = _FakePool(broken=len(built) == 0)
        built.append(pool)
        return pool

    monkeypatch.setattr(webhook_queue, "_create_pool", fake_create_pool,
                        raising=False)
    webhook_queue.reset_pool()

    async with session_maker() as db:
        first = await webhook_queue.enqueue_webhook(db, URL, {"event": "one"})
    async with session_maker() as db:
        second = await webhook_queue.enqueue_webhook(db, URL, {"event": "two"})

    assert len(built) == 2, (
        "после отказа пул обязан строиться заново, а не переиспользоваться мёртвым")
    assert built[1].jobs, "второй вебхук обязан уехать через новый пул"
    assert (await _state(session_maker, first)).status == "pending"
    assert (await _state(session_maker, second)).status == "pending"
    webhook_queue.reset_pool()


@pytest.mark.asyncio
async def test_a_failed_enqueue_still_leaves_the_row_recoverable(session_maker,
                                                                 clean_deliveries,
                                                                 monkeypatch):
    """Отказ очереди не теряет событие: строка остаётся `pending` с нулём попыток."""
    await clean_deliveries()

    async def fake_create_pool(*a, **kw):
        return _FakePool(broken=True)

    monkeypatch.setattr(webhook_queue, "_create_pool", fake_create_pool, raising=False)
    webhook_queue.reset_pool()

    async with session_maker() as db:
        delivery_id = await webhook_queue.enqueue_webhook(db, URL, {"event": "lost?"})

    row = await _state(session_maker, delivery_id)
    assert row.status == "pending" and row.attempts == 0
    webhook_queue.reset_pool()


# ── половина вторая: у обещания «re-driven» появляется исполнитель ────────────

@pytest.mark.asyncio
async def test_a_stranded_delivery_gets_re_driven(session_maker, clean_deliveries):
    """Строка, застрявшая `pending` дольше окна, снова уезжает в очередь."""
    from app.workers.webhook_redrive import (REDRIVE_AFTER_SECONDS,
                                             redrive_pending_webhooks)

    await clean_deliveries()
    stranded = await _add(session_maker,
                          created_at=NOW - timedelta(seconds=REDRIVE_AFTER_SECONDS + 60))
    pool = _FakePool()

    async with session_maker() as db:
        driven = await redrive_pending_webhooks(db, redis=pool, now=NOW)

    assert driven == [stranded], driven
    assert pool.jobs and pool.jobs[0][0] == "deliver_webhook"


@pytest.mark.asyncio
async def test_re_driving_spaces_itself_out(session_maker, clean_deliveries):
    """Второй тик подряд не ставит ту же строку ещё раз.

    Пере-драйвер не трогает `status` (строка и так `pending`), поэтому без отметки
    следующего срока каждый тик слал бы один и тот же вебхук заново, пока получатель
    медленно отвечает.
    """
    from app.workers.webhook_redrive import (REDRIVE_AFTER_SECONDS,
                                             redrive_pending_webhooks)

    await clean_deliveries()
    await _add(session_maker,
               created_at=NOW - timedelta(seconds=REDRIVE_AFTER_SECONDS + 60))
    pool = _FakePool()

    async with session_maker() as db:
        first = await redrive_pending_webhooks(db, redis=pool, now=NOW)
    async with session_maker() as db:
        again = await redrive_pending_webhooks(db, redis=pool,
                                               now=NOW + timedelta(seconds=60))

    assert first and not again, (
        f"повторный тик поставил ту же строку заново: {again}")


@pytest.mark.asyncio
async def test_a_scheduled_retry_is_left_alone(session_maker, clean_deliveries):
    """Штатная отсрочка `deliver_webhook` — не застрявшая строка.

    Обогнать её значило бы доставить один вебхук дважды.
    """
    from app.workers.webhook_redrive import redrive_pending_webhooks

    await clean_deliveries()
    await _add(session_maker, attempts=2,
               next_attempt_at=NOW + timedelta(seconds=120))
    pool = _FakePool()

    async with session_maker() as db:
        driven = await redrive_pending_webhooks(db, redis=pool, now=NOW)

    assert driven == [], driven


@pytest.mark.asyncio
async def test_a_fresh_delivery_is_left_alone(session_maker, clean_deliveries):
    """Только что поставленный вебхук ещё едет — он не застрял."""
    from app.workers.webhook_redrive import redrive_pending_webhooks

    await clean_deliveries()
    await _add(session_maker, created_at=NOW - timedelta(seconds=5))
    pool = _FakePool()

    async with session_maker() as db:
        driven = await redrive_pending_webhooks(db, redis=pool, now=NOW)

    assert driven == [], driven


@pytest.mark.asyncio
async def test_terminal_and_exhausted_rows_are_never_re_driven(session_maker,
                                                               clean_deliveries):
    """`delivered` и `failed` — итог, а исчерпавшая попытки строка не заслуживает
    шестой: иначе пере-драйвер стал бы бесконечным ретраем мимо `MAX_ATTEMPTS`."""
    from app.workers.webhook_redrive import (REDRIVE_AFTER_SECONDS,
                                             redrive_pending_webhooks)

    await clean_deliveries()
    old = NOW - timedelta(seconds=REDRIVE_AFTER_SECONDS + 600)
    await _add(session_maker, status="delivered", created_at=old, delivered_at=old)
    await _add(session_maker, status="failed", attempts=MAX_ATTEMPTS, created_at=old)
    await _add(session_maker, status="pending", attempts=MAX_ATTEMPTS, created_at=old)
    pool = _FakePool()

    async with session_maker() as db:
        driven = await redrive_pending_webhooks(db, redis=pool, now=NOW)

    assert driven == [], driven


@pytest.mark.asyncio
async def test_without_a_pool_it_reports_nothing_driven(session_maker,
                                                        clean_deliveries):
    """Без пула ставить некуда, и возвращать «переставлено» было бы враньём —
    именно такое враньё и прячет отказ доставки."""
    from app.workers.webhook_redrive import (REDRIVE_AFTER_SECONDS,
                                             redrive_pending_webhooks)

    await clean_deliveries()
    await _add(session_maker,
               created_at=NOW - timedelta(seconds=REDRIVE_AFTER_SECONDS + 60))

    async with session_maker() as db:
        driven = await redrive_pending_webhooks(db, redis=None, now=NOW)

    assert driven == [], driven


def test_the_window_cannot_overtake_the_normal_backoff():
    """Условие безопасности, а не деталь: окно короче последнего шага отсрочки
    превратило бы пере-драйвер в источник дублей."""
    from app.workers.webhook_redrive import REDRIVE_AFTER_SECONDS

    assert REDRIVE_AFTER_SECONDS > max(WEBHOOK_BACKOFF), (
        f"{REDRIVE_AFTER_SECONDS} <= {max(WEBHOOK_BACKOFF)}: пере-драйвер обгонит "
        "штатную отсрочку и пошлёт вебхук дважды")


# ── расписание: зарегистрировано И вычисляется ────────────────────────────────

def test_the_redrive_tick_is_registered_on_the_worker():
    from app.workers import arq_settings

    names = [getattr(j.coroutine, "__name__", "") for j in
             arq_settings.WorkerSettings.cron_jobs]
    assert "webhook_redrive_tick" in names, names


def test_the_redrive_schedule_actually_computes():
    """arq принимает в расписании множество или число, но не `range`.

    Крон с `minute=range(...)` проходит «крон зарегистрирован» и сборку образа, а
    воркер падает на первом ударе сердца и уходит в цикл перезапуска (05.09).
    Проверка расписания обязана его ВЫЧИСЛЯТЬ, а не только видеть.
    """
    from app.workers import arq_settings

    job = next(j for j in arq_settings.WorkerSettings.cron_jobs
               if getattr(j.coroutine, "__name__", "") == "webhook_redrive_tick")
    moment = datetime(2026, 9, 6, 12, 0, 0)
    job.calculate_next(moment)
    assert job.next_run is not None
