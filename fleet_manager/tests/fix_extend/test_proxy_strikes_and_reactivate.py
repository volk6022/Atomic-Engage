"""Waking an account up, and not putting it to sleep over a blip.

Both defects here were found together on the vertsanov fleet: five accounts sat in
`sleeping` for five days on a proxy that answered on the first try when finally
checked, and the endpoint meant to wake them refused four of the five.

* The health loop slept an account on ONE failed TCP connect, logged it through a
  stdlib logger the app never configures (so nothing was written in ten days), and
  recorded no telemetry — the row changed and nothing said why.
* `reactivate` re-ran the geo gate without honouring `Account.geo_override`, so the
  flag covered an account right up to the moment it needed waking.

The existing `test_s5_sc2_proxy_fail_triggers_sleeping_alert` reimplements the failover
inline instead of calling `run_health_check_loop`, so it never exercised any of this.
These tests drive the real loop.
"""
import pytest
from sqlalchemy import select

from app.core.constants import AccountStatus
from app.db.models import Account, Proxy, TelemetryEvent
from app.services.proxy_manager import PROXY_FAIL_STRIKES, ProxyManager

H = {"X-API-Key": "change_me_in_production"}

PROXY = "socks5://u__cr.us:p@np.example.com:11000"


class _StopLoop(Exception):
    """Breaks the loop's `while True` after a set number of cycles."""


async def _run_cycles(monkeypatch, session_maker, redis_client, *, results):
    """Run the health loop for `len(results)` cycles, feeding it those check outcomes."""
    import asyncio as _asyncio

    calls = {"n": 0}
    seen = []

    async def _fake_sleep(_seconds):
        if calls["n"] >= len(results):
            raise _StopLoop
        calls["n"] += 1

    async def _fake_check(self, url, timeout=10.0):
        outcome = results[calls["n"] - 1]
        seen.append(outcome)
        return outcome

    async def _no_webhook(self, **kw):
        return True, "stubbed"

    from app.services.webhook_sender import WebhookSender

    monkeypatch.setattr(_asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(ProxyManager, "health_check", _fake_check)
    monkeypatch.setattr(WebhookSender, "send", _no_webhook)

    async with session_maker() as db:
        with pytest.raises(_StopLoop):
            await ProxyManager().run_health_check_loop(db, redis_client)
    return seen


async def _status(session_maker, account_id):
    async with session_maker() as s:
        return (await s.execute(
            select(Account).where(Account.id == account_id))).scalar_one().status


# ── the health loop must tolerate a blip ──────────────────────────────────────

@pytest.mark.asyncio
async def test_one_failed_check_does_not_sleep_the_account(
    account_factory, session_maker, redis_client, monkeypatch
):
    ids = await account_factory(status="active")
    await _run_cycles(monkeypatch, session_maker, redis_client, results=[False])
    assert await _status(session_maker, ids["account_id"]) == AccountStatus.ACTIVE


@pytest.mark.asyncio
async def test_account_sleeps_only_after_the_full_run_of_failures(
    account_factory, session_maker, redis_client, monkeypatch
):
    ids = await account_factory(status="active")
    await _run_cycles(monkeypatch, session_maker, redis_client,
                      results=[False] * PROXY_FAIL_STRIKES)
    assert await _status(session_maker, ids["account_id"]) == AccountStatus.SLEEPING


@pytest.mark.asyncio
async def test_a_recovery_between_failures_clears_the_count(
    account_factory, session_maker, redis_client, monkeypatch
):
    """Two misses, a success, two more misses — never three in a row, so still awake.

    Without the reset a flaky proxy accumulates strikes forever and eventually sleeps
    the fleet, which is indistinguishable from a real outage in the logs.
    """
    ids = await account_factory(status="active")
    await _run_cycles(monkeypatch, session_maker, redis_client,
                      results=[False, False, True, False, False])
    assert await _status(session_maker, ids["account_id"]) == AccountStatus.ACTIVE


@pytest.mark.asyncio
async def test_sleeping_records_why_it_happened(
    account_factory, session_maker, redis_client, monkeypatch
):
    ids = await account_factory(status="active")
    await _run_cycles(monkeypatch, session_maker, redis_client,
                      results=[False] * PROXY_FAIL_STRIKES)

    async with session_maker() as s:
        events = (await s.execute(
            select(TelemetryEvent).where(
                TelemetryEvent.account_id == ids["account_id"],
                TelemetryEvent.event_type == "sleeping"))).scalars().all()

    assert len(events) == 1, "the transition must leave exactly one trace"
    assert "proxy_unreachable" in events[0].cause
    assert f"strikes={PROXY_FAIL_STRIKES}" in events[0].cause


@pytest.mark.asyncio
async def test_a_shared_proxy_row_does_not_kill_the_loop(
    account_factory, session_maker, redis_client, monkeypatch
):
    """Two accounts on one proxy row. `scalar_one_or_none` raised here and took the
    whole fleet's health checking down with it."""
    a = await account_factory(status="active")
    b = await account_factory(status="active")
    async with session_maker() as s:
        acc = (await s.execute(
            select(Account).where(Account.id == b["account_id"]))).scalar_one()
        acc.proxy_id = a["proxy_id"]
        await s.commit()

    await _run_cycles(monkeypatch, session_maker, redis_client,
                      results=[False] * PROXY_FAIL_STRIKES)

    assert await _status(session_maker, a["account_id"]) == AccountStatus.SLEEPING
    assert await _status(session_maker, b["account_id"]) == AccountStatus.SLEEPING


def test_the_strike_count_is_worth_more_than_one_check():
    """A one-strike policy is the defect this file exists for; keep it above 1."""
    assert PROXY_FAIL_STRIKES > 1


# ── waking up ─────────────────────────────────────────────────────────────────

async def _set_override(session_maker, account_id, value):
    async with session_maker() as s:
        acc = (await s.execute(
            select(Account).where(Account.id == account_id))).scalar_one()
        acc.geo_override = value
        await s.commit()


@pytest.mark.asyncio
async def test_reactivate_honours_geo_override(
    async_client, account_factory, session_maker, monkeypatch
):
    """The vertsanov case exactly: a UZ phone on a US proxy, divergence acknowledged
    at onboarding, asleep. It must be possible to wake it through the endpoint."""
    async def _healthy(self, url, timeout=10.0):
        return True

    monkeypatch.setattr(ProxyManager, "health_check", _healthy)

    ids = await account_factory(status="sleeping", phone_country="UZ", proxy_country="US")
    await _set_override(session_maker, ids["account_id"], True)

    r = await async_client.post(
        f"/v1/accounts/{ids['account_id']}/reactivate",
        json={"proxy_url": PROXY, "proxy_country": "US"}, headers=H,
    )
    assert r.status_code == 200, r.text
    assert await _status(session_maker, ids["account_id"]) == AccountStatus.ACTIVE


@pytest.mark.asyncio
async def test_reactivate_still_refuses_a_mismatch_without_the_flag(
    async_client, account_factory, session_maker, monkeypatch
):
    """The gate is not loosened for everyone — only where it was acknowledged."""
    async def _healthy(self, url, timeout=10.0):
        return True

    monkeypatch.setattr(ProxyManager, "health_check", _healthy)

    ids = await account_factory(status="sleeping", phone_country="UZ", proxy_country="US")
    await _set_override(session_maker, ids["account_id"], False)

    r = await async_client.post(
        f"/v1/accounts/{ids['account_id']}/reactivate",
        json={"proxy_url": PROXY, "proxy_country": "US"}, headers=H,
    )
    assert r.status_code == 422
    # The message must name both sides; "geo_mismatch or unknown proxy country" sent
    # the reader looking for a missing country code that was never missing.
    assert "UZ" in r.text and "US" in r.text
    assert await _status(session_maker, ids["account_id"]) == AccountStatus.SLEEPING


@pytest.mark.asyncio
async def test_unknown_country_is_a_different_complaint_than_a_mismatch(
    async_client, account_factory, monkeypatch
):
    async def _healthy(self, url, timeout=10.0):
        return True

    monkeypatch.setattr(ProxyManager, "health_check", _healthy)
    monkeypatch.setattr(ProxyManager, "resolve_country", lambda self, url, hint=None: None)

    ids = await account_factory(status="sleeping", phone_country="US", proxy_country="US")
    r = await async_client.post(
        f"/v1/accounts/{ids['account_id']}/reactivate",
        json={"proxy_url": "socks5://u:p@np.example.com:11000"}, headers=H,
    )
    assert r.status_code == 422
    assert "proxy_country" in r.text and "geo_mismatch" not in r.text
