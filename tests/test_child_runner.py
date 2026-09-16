"""M4 Codex adapter, timeout, and result-ingest acceptance tests."""

import json
import os
import time
from pathlib import Path

import pytest

from orc.child_runner import ChildRunner
from orc.codex_adapter import CodexCapabilityError, CodexExecAdapter
from orc.errors import ChildExecutionError
from orc.stream_capture import ProcessStreamCapture
from orc.worktree import WorktreeManager
from tests.m4_helpers import (
    git,
    make_fake_codex,
    make_owned_worktree,
    make_store,
    process_exists,
    run_fake,
    wait_pid_file,
)
from tests.m6_helpers import make_clean_ingestor


def test_valid_fake_codex_result_is_validated_and_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid final message becomes a checkpointed result.json artifact."""
    store, outcome = run_fake(tmp_path, monkeypatch, "valid")

    artifact = store.run_dir / "tasks/task-1/attempt-1/result.json"
    assert outcome.status == "VALIDATED"
    assert outcome.manager_result is not None
    assert outcome.manager_result["summary"] == "valid bounded summary"
    assert artifact.is_file()
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert json.loads(artifact.read_text(encoding="utf-8"))["task_id"] == "task-1"
    assert store.verify_events()[-1]["type"] == "result_recorded"
    store.verify_integrity()


@pytest.mark.parametrize(
    ("mode", "forbidden_body"),
    [
        ("invalid_json", "UNTRUSTED_BODY_DO_NOT_FORWARD"),
        ("invalid_schema", "UNTRUSTED_SCHEMA_BODY_DO_NOT_FORWARD"),
    ],
)
def test_at5_invalid_json_is_rejected_without_manager_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    forbidden_body: str,
) -> None:
    """Invalid body is reduced to a digest and explicit attempt decision."""
    store, outcome = run_fake(tmp_path, monkeypatch, mode)

    assert outcome.status == "SCHEMA_INVALID"
    assert outcome.manager_result is None
    assert outcome.attempt_consumed is True
    assert outcome.task_state == "RUNNING"
    assert not (store.run_dir / "tasks/task-1/attempt-1/result.json").exists()
    serialized_events = json.dumps(store.verify_events(), ensure_ascii=False)
    assert store.verify_events()[-1]["type"] == "schema_invalid"
    assert forbidden_body not in serialized_events
    assert forbidden_body not in repr(outcome)


def test_schema_invalid_at_hard_cap_is_explicitly_escalated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hard attempt boundary returns an ESCALATED-equivalent result."""
    store, outcome = run_fake(
        tmp_path,
        monkeypatch,
        "invalid_json",
        attempt=1,
        hard_attempts=1,
    )

    assert outcome.task_state == "ESCALATED"
    event = store.verify_events()[-1]
    assert event["type"] == "schema_invalid"
    assert event["data"]["task_state"] == "ESCALATED"


@pytest.mark.parametrize(
    ("mode", "expected_signal", "expected_forced"),
    [
        ("timeout_term", "SIGTERM", False),
        ("timeout_kill", "SIGKILL", True),
    ],
)
def test_at4_timeout_kills_entire_pgid_and_records_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_signal: str,
    expected_forced: bool,
) -> None:
    """Timeout distinguishes graceful TERM from forced KILL and leaves no child."""
    store, outcome = run_fake(tmp_path, monkeypatch, mode, grace=0.1)
    worktree = store.paths.worktree_root / "task-1"
    leader = wait_pid_file(worktree / "leader.pid")
    descendant = wait_pid_file(worktree / "descendant.pid")

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and (process_exists(leader) or process_exists(descendant)):
        time.sleep(0.01)
    assert process_exists(leader) is False
    assert process_exists(descendant) is False
    assert outcome.status == "TIMED_OUT"
    assert outcome.timed_out is True
    assert outcome.termination_signal == expected_signal
    assert outcome.forced_kill is expected_forced
    ledger = json.loads((store.run_dir / "worktrees.json").read_text(encoding="utf-8"))
    record = ledger["processes"][0]
    assert record["exit_code"] == outcome.exit_code
    assert record["timed_out"] is True
    assert record["signal"] == expected_signal
    assert record["status"] == ("TIMED_OUT_KILL" if expected_forced else "TIMED_OUT_TERM")


def test_normal_leader_exit_still_reaps_residual_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful leader cannot leave an inherited-pipe descendant behind."""
    store, outcome = run_fake(tmp_path, monkeypatch, "exit_descendant", grace=0.05)
    descendant = wait_pid_file(
        store.paths.worktree_root / "task-1" / "descendant.pid"
    )

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and process_exists(descendant):
        time.sleep(0.01)

    assert outcome.status == "VALIDATED"
    assert process_exists(descendant) is False


def test_stream_capture_failure_still_finalizes_process_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-exit capture error cannot strand a RUNNING ledger record."""
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    worktree = make_owned_worktree(store)
    executable = make_fake_codex(tmp_path, "valid")
    monkeypatch.setenv("FAKE_EXEC_MARKER", str(worktree.path / "spawned.marker"))
    original_finish = ProcessStreamCapture.finish

    def fail_after_join(self):  # type: ignore[no-untyped-def]
        original_finish(self)
        raise ChildExecutionError("injected capture failure")

    monkeypatch.setattr(ProcessStreamCapture, "finish", fail_after_join)
    runner = ChildRunner(
        store,
        CodexExecAdapter(executable, environment=os.environ.copy()),
        dlp_ingestor=make_clean_ingestor(store),
    )

    with pytest.raises(ChildExecutionError, match="injected capture failure"):
        runner.run(
            worktree,
            task_id="task-1",
            role="implementer",
            attempt=1,
            prompt="bounded",
        )

    ledger = json.loads((store.run_dir / "worktrees.json").read_text(encoding="utf-8"))
    assert ledger["processes"][0]["status"] == "ABORTED"


def test_missing_codex_flags_stop_before_child_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Version drift disables the adapter instead of guessing flags."""
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    worktree = make_owned_worktree(store)
    marker = worktree.path / "spawned.marker"
    monkeypatch.setenv("FAKE_EXEC_MARKER", str(marker))
    executable = make_fake_codex(tmp_path, "valid", complete_help=False)
    runner = ChildRunner(store, CodexExecAdapter(executable))

    with pytest.raises(CodexCapabilityError, match="required flags unavailable"):
        runner.run(
            worktree,
            task_id="task-1",
            role="implementer",
            attempt=1,
            prompt="must not spawn",
        )

    assert marker.exists() is False


def test_attempt_above_manifest_hard_cap_stops_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No child invocation is allowed after the hard attempt is consumed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo, hard_attempts=1)
    worktree = make_owned_worktree(store)
    marker = worktree.path / "spawned.marker"
    monkeypatch.setenv("FAKE_EXEC_MARKER", str(marker))
    runner = ChildRunner(store, CodexExecAdapter(make_fake_codex(tmp_path, "valid")))

    with pytest.raises(ChildExecutionError, match="exceeds manifest"):
        runner.run(
            worktree,
            task_id="task-1",
            role="implementer",
            attempt=2,
            prompt="must not spawn",
        )

    assert marker.exists() is False


def test_user_worktree_status_and_bytes_are_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child writes in its dedicated worktree without touching the parent tree."""
    repo = tmp_path / "user-repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "orc-test")
    git(repo, "config", "user.email", "orc-test@example.invalid")
    tracked = repo / "tracked.txt"
    tracked.write_bytes(b"parent-bytes\n")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-qm", "base")
    head = git(repo, "rev-parse", "HEAD")
    store = make_store(repo)
    worktree = WorktreeManager(repo, "run-1").create("task-1", head)
    fake = make_fake_codex(tmp_path, "valid")
    monkeypatch.setenv("FAKE_EXEC_MARKER", str(worktree.path / "spawned.marker"))
    before_status = git(repo, "status", "--short", "-uall")
    before_bytes = tracked.read_bytes()

    outcome = ChildRunner(
        store,
        CodexExecAdapter(fake, environment=os.environ.copy()),
        dlp_ingestor=make_clean_ingestor(store),
    ).run(
        worktree,
        task_id="task-1",
        role="implementer",
        attempt=1,
        prompt="write only in the dedicated worktree",
    )

    assert outcome.status == "VALIDATED"
    assert git(repo, "status", "--short", "-uall") == before_status
    assert tracked.read_bytes() == before_bytes
    assert (worktree.path / "child-output.txt").read_text(encoding="utf-8") == "child-only"


def test_default_child_environment_excludes_unlisted_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    worktree = make_owned_worktree(store)
    monkeypatch.setenv("ORC_TEST_SECRET", "runtime-only-sensitive-value")

    outcome = ChildRunner(
        store,
        CodexExecAdapter(make_fake_codex(tmp_path, "env_guard")),
        dlp_ingestor=make_clean_ingestor(store),
    ).run(
        worktree,
        task_id="task-1",
        role="implementer",
        attempt=1,
        prompt="verify sanitized environment",
    )

    assert outcome.status == "VALIDATED"


def test_timeout_remains_bounded_when_child_never_reads_large_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, outcome = run_fake(
        tmp_path,
        monkeypatch,
        "timeout_term",
        grace=0.1,
        prompt="x" * (1024 * 1024),
    )

    assert outcome.status == "TIMED_OUT"
    assert outcome.timed_out is True
