"""M4 worktrees.json PGID ledger and orphan recovery tests."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orc.errors import ProcessLedgerError
from orc.lease import LeaseManager
from orc.process_ledger import ProcessLedger
from orc.store import RunStateStore
from tests.helpers import manifest_data


def make_store(repo: Path, run_id: str = "run-1") -> RunStateStore:
    """Create an initialized fenced store."""
    manager = LeaseManager(repo)
    lease = manager.acquire(run_id)
    store = RunStateStore(repo, run_id, manager, lease)
    store.initialize(manifest_data(repo, run_id, lease.fencing_token))
    return store


def owned_worktree(store: RunStateStore, task_id: str = "task-1") -> Path:
    """Create the state-owned worktree directory expected by the ledger."""
    path = store.paths.worktree_root / task_id
    path.mkdir(mode=0o700, parents=True)
    return path


def group_alive(pgid: int) -> bool:
    """Return whether a process group currently exists."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_group_dead(pgid: int) -> None:
    """Allow the OS a short interval to reap a killed test process."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not group_alive(pgid):
            return
        time.sleep(0.01)
    raise AssertionError(f"process group still alive: {pgid}")


def test_at10_live_orphan_pgid_is_recovered_and_event_is_recorded(repo: Path) -> None:
    """A new Manager instance recovers a ledgered child left by a killed Manager."""
    store = make_store(repo)
    worktree = owned_worktree(store)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ],
        cwd=worktree,
        start_new_session=True,
    )
    pgid = os.getpgid(process.pid)
    first_manager = ProcessLedger(store, term_grace_seconds=0.05, poll_interval=0.005)
    first_manager.register(
        task_id="task-1",
        attempt=1,
        pid=process.pid,
        pgid=pgid,
        worktree=worktree,
    )

    recovered = ProcessLedger(
        store,
        term_grace_seconds=0.05,
        poll_interval=0.005,
    ).recover_orphans()

    process.wait(timeout=2)
    wait_group_dead(pgid)
    assert len(recovered) == 1
    assert recovered[0].task_id == "task-1"
    assert recovered[0].signal == "SIGKILL"
    ledger = json.loads((store.run_dir / "worktrees.json").read_text(encoding="utf-8"))
    assert ledger["processes"][0]["status"] == "RECOVERED_KILL"
    events = store.verify_events()
    assert events[-1]["type"] == "orphan_recovered"
    assert events[-1]["data"]["pgid"] == pgid


def test_malformed_ledger_is_not_silently_repaired(repo: Path) -> None:
    """Invalid ledger JSON remains byte-identical and stops startup."""
    store = make_store(repo)
    path = store.run_dir / "worktrees.json"
    original = b'{"processes": [broken'
    path.write_bytes(original)

    with pytest.raises(ProcessLedgerError, match="invalid worktrees ledger JSON"):
        ProcessLedger(store).recover_orphans()

    assert path.read_bytes() == original


def test_pgid_identity_mismatch_is_not_killed_or_rewritten(repo: Path) -> None:
    """A stale/reused PID mismatch is unsafe to kill and fails loudly."""
    store = make_store(repo)
    worktree = owned_worktree(store)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=worktree,
        start_new_session=True,
    )
    ledger = ProcessLedger(store)
    ledger.register(
        task_id="task-1",
        attempt=1,
        pid=process.pid,
        pgid=os.getpgrp(),
        worktree=worktree,
    )

    try:
        with pytest.raises(ProcessLedgerError, match="PID/PGID mismatch"):
            ledger.recover_orphans()
        assert process.poll() is None
    finally:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait(timeout=2)
