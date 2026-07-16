"""Enforced ASN block-list + anti-ring cooldown gates (US3, FR-310/311/312, SC-002).

These prove the guards are WIRED, not just declared: removing the ASN gate makes
`test_asn_*` fail, and removing the cooldown filter makes `test_anti_ring_*` fail.

Requires a real Postgres (db fixtures skip when absent) and kurigram (module skips
when pyrogram is unavailable, since base_task imports it).
"""
import pytest

pytest.importorskip("pyrogram")  # base_task -> _tg_errors -> pyrogram

from datetime import timedelta  # noqa: E402

from sqlalchemy import select, update  # noqa: E402

from app.core.clock import Clock, set_clock  # noqa: E402
from app.core.constants import AccountStatus  # noqa: E402
from app.db.models import Account, Proxy, WarmupCrossPair  # noqa: E402
from app.services.warmup import WarmupPipeline  # noqa: E402
from app.workers.base_task import BaseTask  # noqa: E402


@pytest.mark.asyncio
async def test_asn_block_rejects_account_facing_action(account_factory, session_maker):
    """Account on a datacenter ASN -> account-facing action rejected, account sleeping."""
    ids = await account_factory(phone_country="RU", proxy_country="RU")
    async with session_maker() as db:
        await db.execute(
            update(Proxy).where(Proxy.id == ids["proxy_id"]).values(asn=16509)  # AWS
        )
        await db.commit()

    async with session_maker() as db:
        prep = await BaseTask.prepare({}, ids["account_id"], db, account_facing=True)
        assert prep is None  # rejected
        account = (
            await db.execute(select(Account).where(Account.id == ids["account_id"]))
        ).scalar_one()
        assert account.status == AccountStatus.SLEEPING


@pytest.mark.asyncio
async def test_asn_block_exempts_reads(account_factory, session_maker):
    """A non-account-facing read on the same datacenter ASN is NOT gated."""
    ids = await account_factory(phone_country="RU", proxy_country="RU")
    async with session_maker() as db:
        await db.execute(
            update(Proxy).where(Proxy.id == ids["proxy_id"]).values(asn=16509)
        )
        await db.commit()

    async with session_maker() as db:
        prep = await BaseTask.prepare({}, ids["account_id"], db, account_facing=False)
        assert prep is not None  # reads pass the ASN gate


@pytest.mark.asyncio
async def test_anti_ring_cooldown_excludes_recent_pair(account_factory, session_maker):
    """A pair on a live 48h cooldown is excluded; a different target is allowed."""
    set_clock(Clock(time_scale=1.0))
    src = await account_factory()
    t_recent = await account_factory()
    t_free = await account_factory()
    pipeline = WarmupPipeline()

    async with session_maker() as db:
        db.add(
            WarmupCrossPair(
                source_account_id=src["account_id"],
                target_account_id=t_recent["account_id"],
                action_type="cross_message_reply",
                cooldown_until=Clock(time_scale=1.0).now() + timedelta(days=1),  # in window
            )
        )
        await db.commit()

    async with session_maker() as db:
        pool = [t_recent["account_id"], t_free["account_id"]]
        chosen = await pipeline.select_cross_pair(src["account_id"], db, pool)
        # The on-cooldown target must NOT be chosen; the free one may be.
        assert chosen != t_recent["account_id"]
        assert chosen == t_free["account_id"]


@pytest.mark.asyncio
async def test_anti_ring_gate_coverage_spy(account_factory, session_maker, monkeypatch):
    """SC-002: prepare() must actually consult the ASN block-list. A spy proves the
    guard is invoked, so deleting the call breaks this test."""
    calls = {"asn": 0}

    import app.workers.base_task as bt

    original = bt.is_datacenter_asn

    def _spy(asn):
        calls["asn"] += 1
        return original(asn)

    monkeypatch.setattr(bt, "is_datacenter_asn", _spy)

    ids = await account_factory(phone_country="RU", proxy_country="RU")
    async with session_maker() as db:
        await BaseTask.prepare({}, ids["account_id"], db, account_facing=True)
    assert calls["asn"] >= 1
