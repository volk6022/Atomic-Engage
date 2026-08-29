"""Задача, которую аккаунт не может выполнить, не должна молча держать очередь.

Найдено 28.08 на флоте vertsanov. Один мёртвый порт прокси остановил чтение по
аккаунту на три часа, и снаружи это выглядело как тишина: `POST /v1/action` отдал
`status: "queued"` в момент постановки и больше не сказал ничего.

Механика отказа. `BaseTask.prepare` возвращает None, когда аккаунт спит, забанен или
не проходит гео-гейт. До этой правки задача в таком случае оставалась `QUEUED` — и её
не подбирал уже никто:

* `recover_orphaned_tasks` ищет `EXECUTING` с истёкшей лизой — задача не стартовала;
* `reenqueue_due_deferred` ищет `DEFERRED` с наступившим сроком — срока у неё нет;
* `enqueue_next` зовётся пост-хуком завершившейся задачи — завершаться нечему.

При этом очередь аккаунта строго FIFO, так что застрявшая голова держала и всё, что
стояло за ней. Добивало то, что `reactivate` очередь не пинал: разбуженный аккаунт
оставался с полным залипшим хвостом.

Тесты держат три утверждения: у заблокированной задачи есть срок, у ожидания есть
потолок, а разбуженный аккаунт начинает работать сам.
"""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.constants import AccountStatus, TaskStatus
from app.db.models import Account, Task, WebhookDelivery
from app.services.proxy_manager import ProxyManager

H = {"X-API-Key": "change_me_in_production"}
PROXY = "socks5://u__cr.us:p@np.example.com:11000"


async def _mk_task(session_maker, account_id, *, priority=5, webhook="https://hook.test/r"):
    async with session_maker() as s:
        async with s.begin():
            t = Task(
                external_id=uuid.uuid4().hex,
                account_id=account_id,
                task_type="get_chat_info",
                payload={"username": "somechat"},
                status=TaskStatus.QUEUED,
                webhook_url=webhook,
                priority=priority,
            )
            s.add(t)
            await s.flush()
            return t.id


async def _task(session_maker, task_id):
    async with session_maker() as s:
        return (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()


async def _age_task(session_maker, task_id, seconds):
    """Отодвинуть `created_at` в прошлое — иначе потолок ожидания не проверить."""
    from app.workers.base_task import _now

    async with session_maker() as s:
        t = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        t.created_at = _now() - timedelta(seconds=seconds)
        await s.commit()


def _ctx(session_maker, fake_redis):
    return {"session_maker": session_maker, "redis": fake_redis}


def _never_runs(payload):
    async def _action(client):  # pragma: no cover — вызов означал бы провал теста
        raise AssertionError("действие не должно выполняться у заблокированного аккаунта")

    return _action


# ── у заблокированной задачи появляется срок ──────────────────────────────────

@pytest.mark.asyncio
async def test_a_blocked_task_gets_a_deadline_instead_of_staying_queued(
    account_factory, session_maker, fake_redis
):
    """Главное утверждение файла: `QUEUED` без срока — это и есть тихое зависание."""
    from app.workers.base_task import run_task

    acc = (await account_factory(status="sleeping"))["account_id"]
    task_id = await _mk_task(session_maker, acc)

    out = await run_task(_ctx(session_maker, fake_redis), task_id, _never_runs)

    assert out.get("blocked") is True
    assert out.get("deferred_until"), "срок обязан быть назван в ответе"

    row = await _task(session_maker, task_id)
    assert row.status == TaskStatus.DEFERRED
    assert row.deferred_until is not None, (
        "без `deferred_until` задачу не подберёт ни один существующий подметальщик")
    assert row.error_code == "ACCOUNT_BLOCKED"


@pytest.mark.asyncio
async def test_the_rest_of_the_queue_does_not_stay_stuck_behind_it(
    account_factory, session_maker, fake_redis
):
    """Очередь строго FIFO, поэтому цена застрявшей головы — весь хвост.

    Хвост тоже уходит в `DEFERRED` со сроком: аккаунт заблокирован целиком, и честный
    исход — дать срок каждой задаче, а не одной.
    """
    from app.workers.base_task import run_task

    acc = (await account_factory(status="sleeping"))["account_id"]
    head = await _mk_task(session_maker, acc)
    tail = await _mk_task(session_maker, acc)

    await run_task(_ctx(session_maker, fake_redis), head, _never_runs)
    await run_task(_ctx(session_maker, fake_redis), tail, _never_runs)

    for task_id in (head, tail):
        row = await _task(session_maker, task_id)
        assert row.status == TaskStatus.DEFERRED
        assert row.deferred_until is not None


# ── у ожидания есть потолок ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_waiting_too_long_becomes_a_real_failure(
    account_factory, session_maker, fake_redis
):
    from app.core.config import get_settings
    from app.workers.base_task import run_task

    acc = (await account_factory(status="sleeping"))["account_id"]
    task_id = await _mk_task(session_maker, acc)
    await _age_task(session_maker, task_id, get_settings().TASK_BLOCKED_MAX_SECONDS + 60)

    out = await run_task(_ctx(session_maker, fake_redis), task_id, _never_runs)

    assert out.get("error") == "account_blocked"
    row = await _task(session_maker, task_id)
    assert row.status == TaskStatus.FAILED
    assert row.error_code == "ACCOUNT_BLOCKED"


@pytest.mark.asyncio
async def test_the_caller_is_told_the_work_will_not_happen(
    account_factory, session_maker, fake_redis
):
    """Ради этого всё и затевалось: вызывающий получил «queued» и ничего больше.

    Без вебхука о терминальном исходе прогон на той стороне висит на нуле процентов
    вечно и выглядит как «отправили и не ответили».
    """
    from app.core.config import get_settings
    from app.workers.base_task import run_task

    acc = (await account_factory(status="sleeping"))["account_id"]
    task_id = await _mk_task(session_maker, acc, webhook="https://hook.test/told")
    await _age_task(session_maker, task_id, get_settings().TASK_BLOCKED_MAX_SECONDS + 60)

    await run_task(_ctx(session_maker, fake_redis), task_id, _never_runs)

    async with session_maker() as s:
        sent = (await s.execute(select(WebhookDelivery).where(
            WebhookDelivery.url == "https://hook.test/told"))).scalars().all()

    assert len(sent) == 1, "терминальный исход обязан уехать вызывающему"
    assert sent[0].payload["event"] == "task_failed"
    assert sent[0].payload["error_code"] == "ACCOUNT_BLOCKED"


@pytest.mark.asyncio
async def test_a_task_still_within_the_ceiling_is_not_buried(
    account_factory, session_maker, fake_redis
):
    """Потолок не должен хоронить задачу аккаунта, который просто спит по расписанию."""
    from app.core.config import get_settings
    from app.workers.base_task import run_task

    acc = (await account_factory(status="sleeping"))["account_id"]
    task_id = await _mk_task(session_maker, acc)
    await _age_task(session_maker, task_id, get_settings().TASK_BLOCKED_MAX_SECONDS - 60)

    await run_task(_ctx(session_maker, fake_redis), task_id, _never_runs)

    row = await _task(session_maker, task_id)
    assert row.status == TaskStatus.DEFERRED


# ── разбуженный аккаунт начинает работать сам ─────────────────────────────────

@pytest.mark.asyncio
async def test_reactivate_starts_the_work_that_was_waiting(
    async_client, account_factory, session_maker, monkeypatch
):
    """`reactivate` возвращал аккаунт в `active` и на этом останавливался.

    Очередь двигается только из `enqueue_next`, поэтому разбуженный аккаунт стоял с
    полным хвостом, пока кто-нибудь не ставил задачу в исполнение руками.
    """
    import app.api.v1.accounts as accounts_api

    async def _healthy(self, url, timeout=10.0):
        return True

    monkeypatch.setattr(ProxyManager, "health_check", _healthy)

    # Страны совпадают намеренно: гео-гейт здесь не проверяется, для него есть
    # отдельные тесты в test_proxy_strikes_and_reactivate.py.
    ids = await account_factory(status="sleeping", phone_country="US", proxy_country="US")
    task_id = await _mk_task(session_maker, ids["account_id"])

    enqueued = []

    class _Pool:
        async def enqueue_job(self, task_type, **kw):
            enqueued.append((task_type, kw))

        async def aclose(self):
            pass

    async def _pool(*a, **kw):
        return _Pool()

    monkeypatch.setattr("arq.create_pool", _pool)

    r = await async_client.post(
        f"/v1/accounts/{ids['account_id']}/reactivate",
        json={"proxy_url": PROXY, "proxy_country": "US"}, headers=H,
    )
    assert r.status_code == 200, r.text

    async with session_maker() as s:
        acc = (await s.execute(select(Account).where(
            Account.id == ids["account_id"]))).scalar_one()
        assert acc.status == AccountStatus.ACTIVE

    assert enqueued, "разбудили аккаунт и не тронули очередь — это исходный дефект"
    assert enqueued[0][1]["task_id"] == task_id
    assert accounts_api._wake_queue is not None
