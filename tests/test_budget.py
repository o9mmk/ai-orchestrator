"""M5 AT-2/AT-3 budget meter and enforcement tests."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orc.budget import BudgetLimitReached, BudgetMeter
from orc.child_runner import ChildRunner
from orc.codex_adapter import CodexExecAdapter
from orc.process_control import process_group_alive
from orc.process_ledger import ProcessLedger
from orc.state_machine import RunState
from orc.usage import BudgetSource, UsageValue
from tests.m4_helpers import make_fake_codex, make_owned_worktree, process_exists
from tests.m5_helpers import make_planning_store, move_to_running
from tests.m6_helpers import make_clean_ingestor


class FakeClock:
    """Monotonic injectable clock."""

    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _spawn_group(worktree: Path, *, ignore_term: bool) -> subprocess.Popen[bytes]:
    handler = "signal.signal(signal.SIGTERM, signal.SIG_IGN);" if ignore_term else ""
    return subprocess.Popen(
        [sys.executable, "-c", f"import signal,time;{handler}time.sleep(30)"],
        cwd=worktree,
        start_new_session=True,
    )


def _wait_dead(pid: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and process_exists(pid):
        time.sleep(0.01)
    assert process_exists(pid) is False


def test_budget_counters_are_monotonic_and_awaiting_time_is_not_active(repo: Path) -> None:
    """Calendar time advances while only explicitly active intervals consume active time."""
    # Arrange
    clock = FakeClock()
    store = make_planning_store(repo)
    meter = BudgetMeter(store, clock=clock)

    # Act
    clock.advance(30)
    waiting = meter.snapshot()
    meter.begin_activity("planner-1")
    clock.advance(12)
    meter.end_activity("planner-1")
    meter.reserve_planner()
    meter.add_usage(UsageValue(7, BudgetSource.MEASURED, "test"))
    result = meter.snapshot()

    # Assert
    assert waiting.calendar_seconds == 30
    assert waiting.active_seconds == 0
    assert result.active_seconds == 12
    assert result.calendar_seconds == 42
    assert result.tokens_used == 7
    assert result.child_invocations == 1
    assert result.manager_calls == 1


def test_child_timeout_is_bounded_by_remaining_active_hard_cap(repo: Path) -> None:
    """Active hard time constrains a child even when its role timeout is longer."""
    clock = FakeClock()
    store = make_planning_store(
        repo,
        cap_overrides={"active_seconds_soft": 5, "active_seconds_hard": 10},
    )
    meter = BudgetMeter(store, clock=clock)
    meter.begin_activity("child-1")
    clock.advance(9)

    bounded = meter.bound_timeout(1200)

    assert bounded == 1


def test_at2_soft_budget_stops_new_start_after_running_child_finishes(repo: Path) -> None:
    """Soft cap blocks new work, never kills the current child, then halts pending work."""
    # Arrange
    store = make_planning_store(
        repo,
        cap_overrides={"child_invocations_soft": 1, "child_invocations_hard": 3},
    )
    move_to_running(store)
    worktree = store.paths.worktree_root / "task-1"
    worktree.mkdir(parents=True)
    process = _spawn_group(worktree, ignore_term=False)
    pgid = os.getpgid(process.pid)
    ledger = ProcessLedger(store, term_grace_seconds=0.05, poll_interval=0.005)
    ledger.register(task_id="task-1", attempt=1, pid=process.pid, pgid=pgid, worktree=worktree)
    meter = BudgetMeter(store)
    meter.reserve_child()

    # Act / Assert
    with pytest.raises(BudgetLimitReached, match="budget_soft"):
        meter.reserve_child()
    assert meter.enforce_soft(pending_tasks=True, running_children=1) is False
    assert process.poll() is None
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=2)
    ledger.complete(
        pid=process.pid,
        pgid=pgid,
        status="EXITED",
        exit_code=process.returncode,
        timed_out=False,
        termination_signal="SIGTERM",
    )
    assert meter.enforce_soft(pending_tasks=True, running_children=0) is True

    events = store.verify_events()
    assert store.read_manifest()["state"] == "HALTED"
    assert any(event["type"] == "budget_soft_halt" for event in events)
    assert all(event["type"] != "budget_child_terminated" for event in events)


def test_soft_budgeted_child_runner_stops_before_fake_spawn(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ChildRunner connection applies the meter before adapter.spawn."""
    store = make_planning_store(
        repo,
        cap_overrides={"child_invocations_soft": 1, "child_invocations_hard": 2},
    )
    worktree = make_owned_worktree(store)
    marker = worktree.path / "spawned.marker"
    monkeypatch.setenv("FAKE_EXEC_MARKER", str(marker))
    meter = BudgetMeter(store)
    meter.reserve_child()
    runner = ChildRunner(
        store,
        CodexExecAdapter(make_fake_codex(tmp_path, "valid"), environment=os.environ.copy()),
        budget=meter,
        dlp_ingestor=make_clean_ingestor(store),
    )

    with pytest.raises(BudgetLimitReached, match="budget_soft"):
        runner.run(
            worktree,
            task_id="task-1",
            role="implementer",
            attempt=1,
            prompt="must not spawn",
        )

    assert marker.exists() is False


def test_budgeted_child_runner_halts_when_completed_usage_hits_hard_cap(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed child's final usage still drives the RUNNING run to HALTED."""
    store = make_planning_store(
        repo,
        cap_overrides={"tokens_soft": 1, "tokens_hard": 1},
    )
    move_to_running(store)
    worktree = make_owned_worktree(store)
    monkeypatch.setenv("FAKE_EXEC_MARKER", str(worktree.path / "spawned.marker"))
    runner = ChildRunner(
        store,
        CodexExecAdapter(make_fake_codex(tmp_path, "valid"), environment=os.environ.copy()),
        budget=BudgetMeter(store),
        dlp_ingestor=make_clean_ingestor(store),
    )

    outcome = runner.run(
        worktree,
        task_id="task-1",
        role="implementer",
        attempt=1,
        prompt="produce a valid bounded result",
    )

    assert outcome.status == "VALIDATED"
    assert store.read_manifest()["state"] == RunState.HALTED.value
    assert any(event["type"] == "budget_hard_halt" for event in store.verify_events())


def test_at3_hard_budget_kills_all_ledgered_groups_and_checkpoints(repo: Path) -> None:
    """Hard cap distinguishes TERM/KILL evidence and leaves no process group."""
    # Arrange
    store = make_planning_store(repo, cap_overrides={"tokens_soft": 5, "tokens_hard": 10})
    move_to_running(store)
    ledger = ProcessLedger(store, term_grace_seconds=0.05, poll_interval=0.005)
    processes: list[subprocess.Popen[bytes]] = []
    pgids: list[int] = []
    for task_id, ignore_term in (("term-task", False), ("kill-task", True)):
        worktree = store.paths.worktree_root / task_id
        worktree.mkdir(parents=True)
        process = _spawn_group(worktree, ignore_term=ignore_term)
        processes.append(process)
        pgids.append(os.getpgid(process.pid))
        ledger.register(
            task_id=task_id,
            attempt=1,
            pid=process.pid,
            pgid=os.getpgid(process.pid),
            worktree=worktree,
        )
    meter = BudgetMeter(store)
    meter.add_usage(UsageValue(10, BudgetSource.BYTES_PROXY, "test_bytes"))

    # Act
    evidence = meter.enforce_hard(ledger)

    # Assert
    for process in processes:
        process.wait(timeout=2)
        _wait_dead(process.pid)
    assert all(process_group_alive(pgid) is False for pgid in pgids)
    assert {item.signal for item in evidence} == {"SIGTERM", "SIGKILL"}
    assert {item.status for item in evidence} == {"BUDGET_TERM", "BUDGET_KILL"}
    assert all(hasattr(item, "exit_code") for item in evidence)
    with pytest.raises(BudgetLimitReached, match="budget_hard"):
        meter.reserve_child()
    assert store.read_manifest()["state"] == "HALTED"
    checkpoint = json.loads(store.checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["budget"]["tokens_used"] == 10
    assert checkpoint["budget"]["budget_source"] == "bytes_proxy"
    assert checkpoint["budget"]["hard_reached"] is True
    events = store.verify_events()
    assert sum(event["type"] == "budget_child_terminated" for event in events) == 2
    assert any(event["type"] == "budget_hard_halt" for event in events)


def test_proxy_usage_never_appears_as_measured(repo: Path) -> None:
    """A later proxy sample downgrades aggregate source confidence."""
    store = make_planning_store(repo, budget_source="measured")
    meter = BudgetMeter(store)

    meter.add_usage(UsageValue(3, BudgetSource.MEASURED, "validated"))
    meter.add_usage(UsageValue(4, BudgetSource.BYTES_PROXY, "bytes"))

    assert meter.snapshot().budget_source is BudgetSource.BYTES_PROXY
    checkpoint = json.loads(store.checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["budget"]["budget_source"] == "bytes_proxy"
