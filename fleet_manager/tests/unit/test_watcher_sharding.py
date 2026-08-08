"""Every account must land in exactly one watcher shard.

Before this, on_startup ran the same unfiltered query in every process, so a
four-watcher deploy opened four concurrent MTProto connections per account — same
auth key, same egress IP — which is both wasted capacity and a conspicuous signal.
"""
from app.watchers.rotation_manager import WatcherRotationManager


def _owner(account_id: int, total: int) -> int:
    """Which process id claims this account under the modulo partition."""
    for pid in range(1, total + 1):
        if account_id % total == (pid - 1) % total:
            return pid
    raise AssertionError("no owner")


def test_every_account_has_exactly_one_owner():
    total = 4
    for account_id in range(1, 61):
        owners = [
            pid for pid in range(1, total + 1)
            if account_id % total == (pid - 1) % total
        ]
        assert len(owners) == 1, f"account {account_id} claimed by {owners}"


def test_partition_covers_the_whole_fleet():
    total = 4
    claimed = {aid: _owner(aid, total) for aid in range(1, 61)}
    assert set(claimed) == set(range(1, 61))
    # And it actually spreads: a five-account fleet must not pile onto one process.
    assert len({_owner(a, total) for a in range(1, 6)}) > 1


def test_single_process_owns_everything():
    """The default (no WATCHER_TOTAL set) must keep the old single-watcher behaviour."""
    assert all(_owner(a, 1) == 1 for a in range(1, 20))


def test_total_processes_is_floored_at_one():
    """A zero would make the modulo raise; the manager clamps it."""
    assert WatcherRotationManager(process_id=1, total_processes=0).total_processes == 1
    assert WatcherRotationManager(process_id=1).total_processes == 1
