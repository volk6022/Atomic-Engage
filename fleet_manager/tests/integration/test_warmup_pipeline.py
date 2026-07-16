"""
Story 4: Adaptive Account Warmup Progression
Acceptance scenarios from spec.md §User Story 4
"""
import json
import pytest
import httpx
import respx as respx_lib
from datetime import datetime, timedelta
from sqlalchemy import select

from app.db.models import Account, WarmupCrossPair
from app.core.constants import WarmupTier
from app.services.warmup import WarmupPipeline

N8N_WEBHOOK_URL = "https://your-n8n-instance.com/webhook/fleet"


@pytest.mark.asyncio
async def test_s4_sc1_fresh_tier_actions_only_no_cold_messages(account_factory, session_maker):
    """
    Given an account at warmup_tier='fresh',
    When the system requests allowed actions,
    Then only non-outbound actions are permitted and send_message is excluded.
    """
    ids = await account_factory(
        status="warmup",
        warmup_tier="fresh",
        use_case="reactions",
    )
    account_id = ids["account_id"]

    async with session_maker() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()

        pipeline = WarmupPipeline()
        allowed = pipeline.get_allowed_actions(account)

    assert "send_message" not in allowed, (
        "send_message must NOT be allowed for fresh tier accounts"
    )
    assert len(allowed) > 0, "There must be some allowed warmup actions for fresh tier"
    cold_outbound = {"cold_dm", "invite_to_group"}
    for action in allowed:
        assert action not in cold_outbound, (
            f"Cold outbound action '{action}' must not be permitted at fresh tier"
        )


@pytest.mark.asyncio
async def test_s4_sc2_basic_to_intermediate_transition_webhook(account_factory, session_maker):
    """
    Given an account that has completed the 'basic' phase for use_case='cold_dm',
    When the system checks for tier advancement (warmup_day ≥ intermediate threshold),
    Then the account transitions to 'intermediate' and n8n receives a phase-change webhook.
    """
    from app.services.webhook_sender import WebhookSender

    ids = await account_factory(
        status="warmup",
        warmup_tier="basic",
        use_case="cold_dm",
        warmup_day=15,  # cold_dm basic threshold is 14 days
    )
    account_id = ids["account_id"]

    with respx_lib.mock(assert_all_mocked=False) as mock:
        webhook_route = mock.post(N8N_WEBHOOK_URL).mock(return_value=httpx.Response(200))

        async with session_maker() as session:
            result = await session.execute(select(Account).where(Account.id == account_id))
            account = result.scalar_one_or_none()

            pipeline = WarmupPipeline()
            new_tier = await pipeline.advance_tier_if_due(account, session, WebhookSender())

    assert new_tier is not None, "Tier should have advanced"
    assert new_tier == WarmupTier.INTERMEDIATE

    async with session_maker() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        updated = result.scalar_one_or_none()

    assert updated.warmup_tier == WarmupTier.INTERMEDIATE

    assert webhook_route.called, "warmup_transition webhook must be fired"
    body = json.loads(webhook_route.calls[0].request.content)
    assert body["event"] in ("warmup_transition", "warmup_complete")
    assert body["account_id"] == account_id
    assert body["to_tier"] == WarmupTier.INTERMEDIATE


@pytest.mark.asyncio
async def test_s4_sc3_tree_pattern_anti_ring(account_factory, session_maker):
    """
    Given accounts A→B and B→C cross-message pairs already exist,
    When the scheduler tries to assign C→A (which would form a ring),
    Then select_cross_pair() rejects A as target for C,
    and the existing pairs have cooldown_until set (2-day cooldown enforced).
    """
    ids_a = await account_factory(status="warmup", warmup_tier="basic", use_case="reactions")
    ids_b = await account_factory(status="warmup", warmup_tier="basic", use_case="reactions")
    ids_c = await account_factory(status="warmup", warmup_tier="basic", use_case="reactions")

    a_id = ids_a["account_id"]
    b_id = ids_b["account_id"]
    c_id = ids_c["account_id"]

    cooldown = datetime.utcnow() + timedelta(days=2)
    async with session_maker() as session:
        async with session.begin():
            session.add_all([
                WarmupCrossPair(
                    source_account_id=a_id,
                    target_account_id=b_id,
                    action_type="cross_message_reply",
                    cooldown_until=cooldown,
                ),
                WarmupCrossPair(
                    source_account_id=b_id,
                    target_account_id=c_id,
                    action_type="cross_message_reply",
                    cooldown_until=cooldown,
                ),
            ])

    pipeline = WarmupPipeline()
    async with session_maker() as session:
        target = await pipeline.select_cross_pair(c_id, session, [a_id, b_id, c_id])

    assert target != a_id, (
        "C→A must be rejected: it would form a ring A→B→C→A"
    )

    async with session_maker() as session:
        result = await session.execute(
            select(WarmupCrossPair).where(
                WarmupCrossPair.source_account_id == a_id,
                WarmupCrossPair.target_account_id == b_id,
            )
        )
        pair = result.scalar_one_or_none()

    assert pair is not None, "A→B pair must exist in DB"
    assert pair.cooldown_until is not None, "Pair must have a 2-day cooldown_until set"
