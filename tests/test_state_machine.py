"""設計書§6の状態遷移表を固定するテスト。"""

import pytest

from orc.errors import InvalidTransition
from orc.state_machine import RunState, RunStateMachine, TaskState, TaskStateMachine


@pytest.mark.parametrize(
    ("current", "event", "expected"),
    [
        (RunState.INIT, "lease_acquired", RunState.PREFLIGHT1),
        (RunState.INIT, "lease_contended", RunState.REFUSED),
        (RunState.PREFLIGHT1, "checks_passed", RunState.PLANNING),
        (RunState.PREFLIGHT1, "checks_refused", RunState.REFUSED),
        (RunState.PLANNING, "plan_retry", RunState.PLANNING),
        (RunState.PLANNING, "plan_valid", RunState.PREFLIGHT2),
        (RunState.PLANNING, "plan_failed", RunState.FAILED),
        (RunState.PREFLIGHT2, "dirty_overlap", RunState.REFUSED),
        (RunState.PREFLIGHT2, "worktree_unavailable", RunState.REFUSED),
        (RunState.PREFLIGHT2, "size_xl", RunState.HALTED),
        (RunState.PREFLIGHT2, "size_l", RunState.AWAITING_START_APPROVAL),
        (RunState.PREFLIGHT2, "size_sm", RunState.RUNNING),
        (RunState.AWAITING_START_APPROVAL, "approve", RunState.RUNNING),
        (RunState.AWAITING_START_APPROVAL, "reject", RunState.CANCELLED),
        (RunState.RUNNING, "tasks_done", RunState.INTEGRATING),
        (RunState.RUNNING, "no_completed_tasks", RunState.FAILED),
        (RunState.RUNNING, "budget_soft_remaining", RunState.HALTED),
        (RunState.RUNNING, "budget_hard", RunState.HALTED),
        (RunState.RUNNING, "tamper_detected", RunState.HALTED),
        (RunState.RUNNING, "lease_lost", RunState.HALTED),
        (RunState.INTEGRATING, "integration_ready", RunState.AWAITING_APPROVAL),
        (RunState.INTEGRATING, "stale_head", RunState.AWAITING_APPROVAL),
        (RunState.AWAITING_APPROVAL, "approve", RunState.COMPLETED),
        (RunState.AWAITING_APPROVAL, "reject", RunState.CANCELLED),
        (RunState.CANCELLING, "children_stopped", RunState.CANCELLED),
    ],
)
def test_run_transition_table(
    current: RunState,
    event: str,
    expected: RunState,
) -> None:
    """§6.1で列挙された各事象が決められた次状態へ進む。"""
    machine = RunStateMachine(current)

    result = machine.transition(event)

    assert result is expected


@pytest.mark.parametrize("state", [state for state in RunState if not state.terminal])
def test_cancel_is_available_from_every_nonterminal_state(state: RunState) -> None:
    """任意の非終端状態でcancel要求をCANCELLINGへ遷移させる。"""
    machine = RunStateMachine(state)

    result = machine.transition("cancel_requested")

    assert result is RunState.CANCELLING


def test_terminal_state_rejects_transition() -> None:
    """終端runの直接再開を状態機械で拒否する。"""
    machine = RunStateMachine(RunState.HALTED)

    with pytest.raises(InvalidTransition):
        machine.transition("lease_acquired")


def test_task_retry_caps_and_flaky_escalation() -> None:
    """taskのretry/review/flaky上限を決定論的に適用する。"""
    task = TaskStateMachine(TaskState.VERIFYING, attempt=3)

    regression = task.transition("regression")

    assert regression is TaskState.ESCALATED
    assert TaskStateMachine(TaskState.VERIFYING).transition("flaky") is TaskState.ESCALATED


def test_second_review_rejection_escalates_before_third_implementer() -> None:
    """review-fix hard 2で3周目のImplementer起動を防ぐ。"""
    task = TaskStateMachine(TaskState.REVIEWING, review_cycles=1)

    result = task.transition("request_changes")

    assert result is TaskState.ESCALATED
    assert task.review_cycles == 2


def test_context_request_does_not_consume_attempt_and_is_capped() -> None:
    """追加contextはattemptを消費せず、taskごとのhard 2回で閉じる。"""
    task = TaskStateMachine(TaskState.RUNNING, attempt=1)

    task.transition("context_request")
    task.transition("context_request")
    result = task.transition("context_request")

    assert task.attempt == 1
    assert result is TaskState.ESCALATED
