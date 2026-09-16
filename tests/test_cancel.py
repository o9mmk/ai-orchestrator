"""M8 cancel process-group and retention acceptance tests."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from orc.cancel import CancelService
from orc.process_ledger import ProcessLedger
from tests.m8_helpers import make_committed_repo, make_running_store


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def test_at13_cancel_kills_all_children_and_retains_worktree_artifacts(
    tmp_path: Path,
) -> None:
    repo = make_committed_repo(tmp_path / "repo")
    store = make_running_store(repo)
    worktree = store.paths.worktree_root / "task-1"
    worktree.mkdir(parents=True)
    retained = worktree / "candidate.txt"
    retained.write_text("retain me\n", encoding="utf-8")
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
    ProcessLedger(store).register(
        task_id="task-1",
        attempt=1,
        pid=process.pid,
        pgid=pgid,
        worktree=worktree,
    )
    store.record_task_state(
        "task-1", {"state": "RUNNING", "attempt": 1, "review_cycles": 0}
    )

    outcome = CancelService(
        store, term_grace_seconds=0.05, poll_interval=0.005
    ).cancel()

    process.wait(timeout=2)
    deadline = time.monotonic() + 2
    while _group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.01)
    ledger = json.loads((store.run_dir / "worktrees.json").read_text(encoding="utf-8"))
    checkpoint = store.verify_integrity()
    assert outcome.state == "CANCELLED"
    assert outcome.terminated_children == 1
    assert _group_alive(pgid) is False
    assert ledger["processes"][0]["status"] == "CANCEL_KILL"
    assert checkpoint.tasks["task-1"]["state"] == "ABORTED"
    assert retained.read_text(encoding="utf-8") == "retain me\n"
    assert worktree.exists()
