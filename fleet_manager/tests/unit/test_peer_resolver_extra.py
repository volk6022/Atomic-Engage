"""Additional unit tests for PeerResolver.upsert_peer coverage."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.peer_resolver import PeerResolver


@pytest.fixture
def resolver():
    return PeerResolver()


def _make_db(first_result=None, second_result=None):
    """Build a db AsyncMock with two execute() results."""
    db = AsyncMock()
    db.add = MagicMock()  # sync call in SQLAlchemy
    db.flush = AsyncMock()

    r1 = MagicMock()
    r1.scalar_one_or_none.return_value = first_result
    r2 = MagicMock()
    r2.scalar_one_or_none.return_value = second_result
    db.execute.side_effect = [r1, r2]
    return db


@pytest.mark.asyncio
async def test_upsert_peer_creates_new_global_peer_and_access_hash(resolver):
    db = _make_db(first_result=None, second_result=None)

    await resolver.upsert_peer(db, 555, "user123", "hash_abc", 1)

    # GlobalPeer added + PeerAccessHash added
    assert db.add.call_count == 2
    assert db.flush.await_count == 2


@pytest.mark.asyncio
async def test_upsert_peer_existing_peer_new_access_hash(resolver):
    existing_peer = MagicMock()
    existing_peer.id = 99
    db = _make_db(first_result=existing_peer, second_result=None)

    await resolver.upsert_peer(db, 555, "user123", "hash_abc", 1)

    # Only PeerAccessHash added (GlobalPeer already exists)
    assert db.add.call_count == 1
    assert db.flush.await_count == 1


@pytest.mark.asyncio
async def test_upsert_peer_skips_duplicate_access_hash(resolver):
    existing_peer = MagicMock()
    existing_peer.id = 99
    existing_hash = MagicMock()
    db = _make_db(first_result=existing_peer, second_result=existing_hash)

    await resolver.upsert_peer(db, 555, "user123", "hash_abc", 1)

    # Nothing added since both exist
    db.add.assert_not_called()
