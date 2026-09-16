"""Cross-terminal cooperative cancel intent acceptance tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from orc.cancel_intent import CancelIntent, finalize_pending_cancel
from orc.child_runner import ChildRunner
from orc.cli import main
from orc.codex_adapter import CodexExecAdapter
from orc.lease import LeaseManager
from orc.session import release_run
from orc.store import RunStateStore
from tests.helpers import manifest_data
from tests.m4_helpers import make_fake_codex, make_owned_worktree, make_store
from tests.m8_helpers import make_committed_repo, make_running_store


def test_live_owner_consumes_cancel_intent_and_stops_child_group(
    repo: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    store = make_store(repo, timeout=20)
    worktree = make_owned_worktree(store)
    fake = make_fake_codex(tmp_path, "timeout_term")
    leader = tmp_path / "leader.pid"
    descendant = tmp_path / "descendant.pid"
    monkeypatch.setenv("FAKE_LEADER_PID", str(leader))
    monkeypatch.setenv("FAKE_DESCENDANT_PID", str(descendant))
    runner = ChildRunner(
        store,
        CodexExecAdapter(fake, environment=os.environ.copy()),
        term_grace_seconds=0.05,
        poll_interval=0.005,
    )
    result: list[object] = []

    thread = threading.Thread(
        target=lambda: result.append(
            runner.run(
                worktree,
                task_id="task-1",
                role="implementer",
                attempt=1,
                prompt="bounded prompt",
            )
        )
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not leader.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert leader.exists()

    request_started = time.monotonic()
    cancelled = subprocess.run(
        [sys.executable, "-m", "orc", "cancel", "--repo", str(repo), store.run_id],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env=os.environ.copy(),
    )
    assert cancelled.returncode == 0, cancelled.stderr
    assert json.loads(cancelled.stdout)["state"] == "CANCEL_PENDING"
    intent_path = CancelIntent(repo, store.run_id).path
    assert (intent_path.stat().st_mode & 0o777) == 0o600

    thread.join(timeout=5)
    assert thread.is_alive() is False
    assert time.monotonic() - request_started < 5
    assert result[0].status == "CANCELLED"  # type: ignore[union-attr]
    assert store.read_manifest()["state"] == "CANCELLED"
    assert store.verify_integrity().run_state == "CANCELLED"
    assert intent_path.exists() is False


def test_cancel_intent_request_is_idempotent_for_same_generation(
    repo: Path,
) -> None:
    store = make_store(repo)
    intent = CancelIntent(repo, store.run_id)

    first = intent.request(store.fencing_token)
    second = intent.request(store.fencing_token)

    assert first.created is True
    assert second.created is False
    assert first.path == second.path


def test_cancel_cli_returns_bounded_pending_for_live_owner_and_double_cancel(
    repo: Path,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    store = make_store(repo)

    assert main(["cancel", "--repo", str(repo), store.run_id]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first == {
        "run_id": store.run_id,
        "state": "CANCEL_PENDING",
        "terminated_children": 0,
    }
    assert main(["cancel", "--repo", str(repo), store.run_id]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second == first

    assert finalize_pending_cancel(store) is True
    assert store.read_manifest()["state"] == "CANCELLED"
    assert main(["cancel", "--repo", str(repo), store.run_id]) == 0
    terminal = json.loads(capsys.readouterr().out)
    assert terminal["state"] == "CANCELLED"


def test_pending_cancel_wins_over_approval(
    tmp_path: Path,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    repo = make_committed_repo(tmp_path / "approval-repo")
    store = make_running_store(repo)
    store.transition("tasks_done")
    store.transition("integration_ready")
    token = store.fencing_token
    release_run(store)
    CancelIntent(repo, store.run_id).request(token)

    assert main(["approve", "--repo", str(repo), store.run_id]) == 0

    outcome = json.loads(capsys.readouterr().out)
    assert outcome["state"] == "CANCELLED"
    assert outcome["terminated_children"] == 0


def test_dead_owner_is_reclaimed_inside_ttl_and_cancelled(
    repo: Path,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    dead_pid = 2_147_483_647
    manager = LeaseManager(repo, pid=dead_pid, pid_alive=lambda _pid: False)
    lease = manager.acquire("run-dead")
    store = RunStateStore(repo, "run-dead", manager, lease)
    store.initialize(manifest_data(repo, "run-dead", lease.fencing_token))

    assert main(["cancel", "--repo", str(repo), "run-dead"]) == 0

    outcome = json.loads(capsys.readouterr().out)
    assert outcome["state"] == "CANCELLED"
    assert outcome["terminated_children"] == 0


def test_terminal_run_discards_valid_leftover_intent_without_mutating_run(
    repo: Path,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    store = make_store(repo)
    store.transition("cancel_requested")
    store.transition("children_stopped")
    token = store.fencing_token
    release_run(store)
    leftover = CancelIntent(repo, store.run_id).request(token).path
    checkpoint_before = store.checkpoint_path.read_bytes()

    assert main(["cancel", "--repo", str(repo), store.run_id]) == 0

    assert json.loads(capsys.readouterr().out)["state"] == "CANCELLED"
    assert leftover.exists() is False
    assert store.checkpoint_path.read_bytes() == checkpoint_before
