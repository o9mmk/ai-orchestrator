"""repo path lockの重複とfencingを検証する。"""

from pathlib import Path

import pytest

from orc.errors import LeaseLost, PathLockConflict
from orc.lease import LeaseManager
from orc.path_locks import PathLockManager


def test_overlapping_parent_child_scopes_are_refused(repo: Path) -> None:
    """別taskの親子scope重複を拒否する。"""
    lease_manager = LeaseManager(repo)
    lease = lease_manager.acquire("run-1")
    locks = PathLockManager(lease_manager, lease)
    locks.acquire("task-1", ["src/orc"])

    with pytest.raises(PathLockConflict, match="path_lock_conflict"):
        locks.acquire("task-2", ["src/orc/store.py"])


def test_old_fence_cannot_update_path_locks(repo: Path) -> None:
    """lease交代後の旧Managerによるpath lock更新を拒否する。"""
    clock = [1_000.0]
    first_manager = LeaseManager(
        repo,
        pid=1234,
        clock=lambda: clock[0],
        pid_alive=lambda _pid: False,
    )
    first_lease = first_manager.acquire("run-1", ttl_seconds=60)
    old_locks = PathLockManager(first_manager, first_lease)
    clock[0] += 61
    second_manager = LeaseManager(
        repo,
        pid=5678,
        clock=lambda: clock[0],
        pid_alive=lambda _pid: False,
    )
    second_manager.acquire("run-2", ttl_seconds=60)

    with pytest.raises(LeaseLost, match="lease_lost"):
        old_locks.acquire("task-1", ["src"])
