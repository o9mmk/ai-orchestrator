"""Committed repo and terminal run fixtures for M8."""

import subprocess
from pathlib import Path

from orc.lease import LeaseManager
from orc.store import RunStateStore
from tests.helpers import manifest_data


def git(repo: Path, *args: str) -> str:
    """Run one local-only git command in a fixture repo."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_committed_repo(path: Path) -> Path:
    """Create a one-file main branch with deterministic identity."""
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "orc-test")
    git(path, "config", "user.email", "orc-test@example.invalid")
    (path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(path, "add", "app.py")
    git(path, "commit", "-m", "initial")
    return path


def make_running_store(repo: Path, run_id: str = "run-1") -> RunStateStore:
    """Create a fenced run positioned at RUNNING on the real fixture HEAD."""
    manager = LeaseManager(repo)
    lease = manager.acquire(run_id)
    manifest = manifest_data(repo, run_id, lease.fencing_token)
    manifest["base_commit"] = git(repo, "rev-parse", "HEAD")
    store = RunStateStore(repo, run_id, manager, lease)
    store.initialize(manifest)
    store.transition("lease_acquired")
    store.transition("checks_passed")
    store.transition("plan_valid")
    store.transition("size_sm")
    return store


def make_halted_store(repo: Path, run_id: str = "run-1") -> RunStateStore:
    """Create a terminal source run with one reusable DONE task artifact."""
    store = make_running_store(repo, run_id)
    store.record_task_state(
        "task-1",
        {"state": "DONE", "attempt": 1, "review_cycles": 0},
    )
    patch = store.run_dir / "tasks/task-1/attempt-1/patch.diff"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text("safe reusable patch\n", encoding="utf-8")
    store.write_checkpoint(run_state="RUNNING")
    store.transition("budget_hard", reason="fixture_halt")
    return store


def run_files(run_dir: Path) -> dict[str, bytes]:
    """Return exact source-run bytes for no-repair assertions."""
    return {
        path.relative_to(run_dir).as_posix(): path.read_bytes()
        for path in sorted(run_dir.rglob("*"))
        if path.is_file()
    }
