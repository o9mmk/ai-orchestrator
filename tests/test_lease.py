"""設計書AT-17 lease/fencingのテスト。"""

from pathlib import Path

import pytest

from orc.errors import LeaseHeld, LeaseLost
from orc.lease import LeaseManager
from orc.store import RunStateStore
from tests.helpers import manifest_data


def test_at17_second_run_is_refused_while_first_lease_is_live(repo: Path) -> None:
    """同一repoの後発runは課金前にlease競合で拒否される。"""
    first = LeaseManager(repo, pid=1234, pid_alive=lambda _pid: True)
    second = LeaseManager(repo, pid=5678, pid_alive=lambda _pid: True)
    first.acquire("run-1")

    with pytest.raises(LeaseHeld, match="lease_held"):
        second.acquire("run-2")


def test_expired_dead_lease_can_be_reclaimed_with_higher_fence(repo: Path) -> None:
    """期限切れかつpid非生存の場合だけ、より大きいtokenで回収する。"""
    clock = [1_000.0]
    first = LeaseManager(repo, pid=1234, clock=lambda: clock[0], pid_alive=lambda _pid: False)
    original = first.acquire("run-1", ttl_seconds=60)
    clock[0] += 61
    second = LeaseManager(repo, pid=5678, clock=lambda: clock[0], pid_alive=lambda _pid: False)

    replacement = second.acquire("run-2", ttl_seconds=60)

    assert replacement.fencing_token > original.fencing_token


def test_dead_lease_is_reclaimed_even_inside_ttl(repo: Path) -> None:
    """死んだownerのleaseはTTL満了を待たず、より大きいtokenで回収する。"""
    clock = [1_000.0]
    first = LeaseManager(repo, pid=1234, clock=lambda: clock[0], pid_alive=lambda _pid: False)
    original = first.acquire("run-1", ttl_seconds=900)
    clock[0] += 1

    replacement = LeaseManager(
        repo,
        pid=5678,
        clock=lambda: clock[0],
        pid_alive=lambda _pid: False,
    ).acquire("run-2", ttl_seconds=900)

    assert replacement.fencing_token > original.fencing_token


def test_fencing_mismatch_immediately_halts_old_store(repo: Path) -> None:
    """旧Managerはfencing不一致を検知した瞬間にHALTEDとなり書込を拒否する。"""
    clock = [1_000.0]
    first_manager = LeaseManager(
        repo,
        pid=1234,
        clock=lambda: clock[0],
        pid_alive=lambda _pid: False,
    )
    first_lease = first_manager.acquire("run-1", ttl_seconds=60)
    store = RunStateStore(repo, "run-1", first_manager, first_lease)
    store.initialize(manifest_data(repo, "run-1", first_lease.fencing_token))
    clock[0] += 61
    second_manager = LeaseManager(
        repo,
        pid=5678,
        clock=lambda: clock[0],
        pid_alive=lambda _pid: False,
    )
    second_manager.acquire("run-2", ttl_seconds=60)

    with pytest.raises(LeaseLost, match="lease_lost"):
        store.append_event("state_transition", "manager", {"to": "PREFLIGHT1"})

    assert store.runtime_state == "HALTED"
    assert store.halt_reason == "lease_lost"


def test_malformed_lease_is_rejected_fail_loud(repo: Path) -> None:
    """lease JSONの値型や範囲を暗黙補正せず拒否する。"""
    manager = LeaseManager(repo)
    lease = manager.acquire("run-1")
    manager.lease_path.write_text(
        f'{{"run_id":"run-1","pid":true,"fencing_token":{lease.fencing_token},'
        '"acquired_at":1,"ttl":900,"renewed_at":1}\n',
        encoding="utf-8",
    )

    with pytest.raises(LeaseLost, match="lease_lost"):
        manager.assert_fencing("run-1", lease.fencing_token)


def test_active_store_renews_within_sixty_seconds_and_fails_loud_on_loss(
    repo: Path,
) -> None:
    clock = [1_000.0]
    manager = LeaseManager(repo, clock=lambda: clock[0])
    lease = manager.acquire("run-1")
    store = RunStateStore(repo, "run-1", manager, lease)
    store.initialize(manifest_data(repo, "run-1", lease.fencing_token))
    clock[0] += 31

    store.maintain_lease()

    assert store.lease.renewed_at == clock[0]
    manager.lease_path.unlink()
    clock[0] += 31
    with pytest.raises(LeaseLost, match="lease_lost"):
        store.maintain_lease()
    assert store.runtime_state == "HALTED"
    assert store.halt_reason == "lease_lost"
