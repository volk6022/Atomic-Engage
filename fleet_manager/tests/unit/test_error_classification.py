"""Task 3.3: separate programmer mistakes from Telegram failures (last-resort branch).

`classify_exception` is pure: the named branches of run_task (FloodWait / BAN_ERRORS /
PeerIdInvalid / CONNECTION_ERRORS) catch theirs earlier and route each to its own
recovery, so only what fell through reaches it. The end-to-end part drives the real
run_task on real Postgres: status=FAILED + error_code + result["error_class"] is a
cross-system contract (Radar translates error_code), so it is asserted against the DB,
not mocks.
"""
from __future__ import annotations

import logging
import uuid

import pytest
from pyrogram import errors as pyrogram_errors
from sqlalchemy import select

from app.core.constants import TaskStatus
from app.db.models import Task, TelemetryEvent
from app.workers import _tg_errors as tg


class _UnknownRpc(pyrogram_errors.BadRequest):
    """An RPCError descendant none of run_task's named branches knows."""

    ID = "UNKNOWN_RPC_TEST"
    MESSAGE = "unknown rpc test"


# ── (а) pure table: exception class → (code, log level) ───────────────────────

@pytest.mark.parametrize(
    "exc_cls",
    [
        AttributeError,
        TypeError,
        KeyError,
        IndexError,
        NameError,
        AssertionError,
        NotImplementedError,
        UnboundLocalError,
    ],
)
def test_programmer_classes_map_to_programmer_error(exc_cls):
    assert exc_cls in tg.PROGRAMMER_ERRORS
    assert tg.classify_exception(exc_cls("boom")) == ("PROGRAMMER_ERROR", "error")


def test_unknown_rpcerror_descendant_maps_to_tg_rpc():
    assert tg.classify_exception(_UnknownRpc(value="42")) == ("TG_RPC", "warning")


def test_other_exception_keeps_its_class_name_as_code():
    assert tg.classify_exception(RuntimeError("boom")) == ("RuntimeError", "warning")


# ── helpers for the end-to-end part ────────────────────────────────────────────

def _boom_action(payload):
    """The builder run_task calls before the (patched) execute raises."""

    async def _action(client):  # pragma: no cover — execute замокан и бросает раньше
        raise AssertionError("не должен вызываться: StatelessManager.execute замокан")

    return _action


def _ctx(session_maker):
    # redis=None: бюджетные и enqueue-гейты считают его отсутствующим; путь
    # падения, который проверяется, redis не касается.
    return {"session_maker": session_maker, "redis": None}


async def _mk_task(session_maker, account_id: int) -> int:
    async with session_maker() as s:
        async with s.begin():
            t = Task(
                external_id=uuid.uuid4().hex,
                account_id=account_id,
                task_type="join_group",
                payload={},
                status=TaskStatus.QUEUED,
                webhook_url=None,  # контракт вебхука 3.3 не менялся и здесь не тест
                priority=5,
            )
            s.add(t)
            await s.flush()
            return t.id


async def _task_row(session_maker, task_id: int) -> Task:
    async with session_maker() as s:
        return (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()


# ── (б) end-to-end: AttributeError в действии → PROGRAMMER_ERROR ──────────────

@pytest.mark.asyncio
async def test_run_task_programmer_error_fails_loudly(
    account_factory, session_maker, monkeypatch, caplog
):
    from app.services.stateless_manager import StatelessManager
    from app.workers.base_task import run_task

    async def _broken(self, account_id, action, db):
        raise AttributeError("'NoneType' object has no attribute 'send_message'")

    monkeypatch.setattr(StatelessManager, "execute", _broken)

    ids = await account_factory(use_case="public_reply")
    task_id = await _mk_task(session_maker, ids["account_id"])

    with caplog.at_level(logging.ERROR, logger="app.workers.base_task"):
        out = await run_task(_ctx(session_maker), task_id, _boom_action)

    assert out.get("error")
    row = await _task_row(session_maker, task_id)
    assert row.status == TaskStatus.FAILED
    assert row.error_code == "PROGRAMMER_ERROR"
    assert row.result["error_class"] == "AttributeError"
    assert "NoneType" in row.result["error"], "текст исключения сохранён в result"

    errors_logged = [
        r
        for r in caplog.records
        if r.name == "app.workers.base_task" and r.levelno == logging.ERROR
    ]
    assert len(errors_logged) == 1, "путь PROGRAMMER_ERROR не молчит"
    assert errors_logged[0].exc_info, "стек попадает в лог (exc_info=True)"

    async with session_maker() as s:
        events = (
            await s.execute(
                select(TelemetryEvent).where(
                    TelemetryEvent.account_id == ids["account_id"],
                    TelemetryEvent.event_type == "programmer_error",
                )
            )
        ).scalars().all()
    assert len(events) == 1, "событие телеметрии programmer_error записано"
    assert events[0].cause == "AttributeError"
    assert events[0].outcome == "failed"


# ── (в) end-to-end: неизвестный RPCError → TG_RPC + warning ────────────────────

@pytest.mark.asyncio
async def test_run_task_unknown_rpc_fails_with_warning_log(
    account_factory, session_maker, monkeypatch, caplog
):
    from app.services.stateless_manager import StatelessManager
    from app.workers.base_task import run_task

    async def _rpc(self, account_id, action, db):
        raise _UnknownRpc(value="42", rpc_name="messages.send")

    monkeypatch.setattr(StatelessManager, "execute", _rpc)

    ids = await account_factory(use_case="public_reply")
    task_id = await _mk_task(session_maker, ids["account_id"])

    with caplog.at_level(logging.WARNING, logger="app.workers.base_task"):
        out = await run_task(_ctx(session_maker), task_id, _boom_action)

    assert out.get("error")
    row = await _task_row(session_maker, task_id)
    assert row.status == TaskStatus.FAILED
    assert row.error_code == "TG_RPC"
    assert row.result["error_class"] == "_UnknownRpc"

    warnings_logged = [
        r
        for r in caplog.records
        if r.name == "app.workers.base_task" and r.levelno == logging.WARNING
    ]
    assert len(warnings_logged) == 1, "путь TG_RPC не молчит"
    message = warnings_logged[0].getMessage()
    assert "_UnknownRpc" in message, "в логе — класс исключения"
    assert "unknown rpc test" in message, "в логе — текст исключения"
