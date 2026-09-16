"""LLM判断を使わないrun/task状態機械。"""

from __future__ import annotations

from enum import StrEnum

from orc.errors import InvalidTransition


class RunState(StrEnum):
    """設計書§6.1のrun状態。"""

    INIT = "INIT"
    PREFLIGHT1 = "PREFLIGHT1"
    PLANNING = "PLANNING"
    PREFLIGHT2 = "PREFLIGHT2"
    AWAITING_START_APPROVAL = "AWAITING_START_APPROVAL"
    RUNNING = "RUNNING"
    INTEGRATING = "INTEGRATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    CANCELLING = "CANCELLING"
    REFUSED = "REFUSED"
    FAILED = "FAILED"
    HALTED = "HALTED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"

    @property
    def terminal(self) -> bool:
        """同一runを再開できない終端状態かを返す。"""
        return self in {
            RunState.REFUSED,
            RunState.FAILED,
            RunState.HALTED,
            RunState.CANCELLED,
            RunState.COMPLETED,
        }


RUN_TRANSITIONS: dict[tuple[RunState, str], RunState] = {
    (RunState.INIT, "lease_acquired"): RunState.PREFLIGHT1,
    (RunState.INIT, "lease_contended"): RunState.REFUSED,
    (RunState.PREFLIGHT1, "checks_passed"): RunState.PLANNING,
    (RunState.PREFLIGHT1, "checks_refused"): RunState.REFUSED,
    (RunState.PLANNING, "plan_retry"): RunState.PLANNING,
    (RunState.PLANNING, "plan_valid"): RunState.PREFLIGHT2,
    (RunState.PLANNING, "plan_failed"): RunState.FAILED,
    (RunState.PLANNING, "goal_too_large"): RunState.REFUSED,
    (RunState.PLANNING, "context_window_unknown"): RunState.REFUSED,
    (RunState.PLANNING, "context_too_large"): RunState.REFUSED,
    (RunState.PLANNING, "budget_soft"): RunState.HALTED,
    (RunState.PLANNING, "budget_hard"): RunState.HALTED,
    (RunState.PREFLIGHT2, "dirty_overlap"): RunState.REFUSED,
    (RunState.PREFLIGHT2, "worktree_unavailable"): RunState.REFUSED,
    (RunState.PREFLIGHT2, "size_xl"): RunState.HALTED,
    (RunState.PREFLIGHT2, "size_l"): RunState.AWAITING_START_APPROVAL,
    (RunState.PREFLIGHT2, "size_sm"): RunState.RUNNING,
    (RunState.AWAITING_START_APPROVAL, "approve"): RunState.RUNNING,
    (RunState.AWAITING_START_APPROVAL, "reject"): RunState.CANCELLED,
    (RunState.RUNNING, "tasks_done"): RunState.INTEGRATING,
    (RunState.RUNNING, "no_completed_tasks"): RunState.FAILED,
    (RunState.RUNNING, "budget_soft_remaining"): RunState.HALTED,
    (RunState.RUNNING, "budget_hard"): RunState.HALTED,
    (RunState.RUNNING, "tamper_detected"): RunState.HALTED,
    (RunState.RUNNING, "lease_lost"): RunState.HALTED,
    (RunState.INTEGRATING, "integration_ready"): RunState.AWAITING_APPROVAL,
    (RunState.INTEGRATING, "stale_head"): RunState.AWAITING_APPROVAL,
    (RunState.AWAITING_APPROVAL, "approve"): RunState.COMPLETED,
    (RunState.AWAITING_APPROVAL, "reject"): RunState.CANCELLED,
    (RunState.CANCELLING, "children_stopped"): RunState.CANCELLED,
}


class RunStateMachine:
    """状態と事象だけから次状態を決定する。"""

    def __init__(self, state: RunState) -> None:
        self.state = state

    def transition(self, event: str) -> RunState:
        """定義済み遷移を適用し、存在しなければ明示エラーにする。"""
        if self.state.terminal:
            raise InvalidTransition(f"terminal run cannot transition: {self.state}")
        if event == "cancel_requested":
            self.state = RunState.CANCELLING
            return self.state
        try:
            self.state = RUN_TRANSITIONS[(self.state, event)]
        except KeyError as error:
            raise InvalidTransition(f"invalid run transition: {self.state}/{event}") from error
        return self.state


class TaskState(StrEnum):
    """設計書§6.2のtask状態。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    FIXING = "FIXING"
    DONE = "DONE"
    ESCALATED = "ESCALATED"
    ABORTED = "ABORTED"


class TaskStateMachine:
    """attempt/review capを含むtask状態機械。"""

    def __init__(
        self,
        state: TaskState,
        *,
        attempt: int = 0,
        review_cycles: int = 0,
        context_requests: int = 0,
    ) -> None:
        self.state = state
        self.attempt = attempt
        self.review_cycles = review_cycles
        self.context_requests = context_requests

    def transition(self, event: str) -> TaskState:
        """§6.2の事象を有限counter付きで処理する。"""
        if self.state in {TaskState.DONE, TaskState.ESCALATED, TaskState.ABORTED}:
            raise InvalidTransition(f"terminal task cannot transition: {self.state}")
        if event in {"run_cancelled", "run_halted"}:
            self.state = TaskState.ABORTED
        elif event == "context_request":
            self.context_requests += 1
            if self.context_requests > 2:
                self.state = TaskState.ESCALATED
        elif self.state is TaskState.QUEUED and event == "start":
            self.state = TaskState.RUNNING
        elif self.state is TaskState.RUNNING and event == "child_succeeded":
            self.state = TaskState.VERIFYING
        elif self.state is TaskState.VERIFYING and event == "verified":
            self.state = TaskState.REVIEWING
        elif self.state is TaskState.VERIFYING and event == "flaky":
            self.state = TaskState.ESCALATED
        elif self.state is TaskState.VERIFYING and event == "regression":
            self.attempt += 1
            self.state = TaskState.ESCALATED if self.attempt > 3 else TaskState.RUNNING
        elif self.state is TaskState.REVIEWING and event == "approved":
            self.state = TaskState.DONE
        elif self.state is TaskState.REVIEWING and event == "request_changes":
            self.review_cycles += 1
            self.state = TaskState.ESCALATED if self.review_cycles >= 2 else TaskState.FIXING
        elif self.state is TaskState.FIXING and event == "retry":
            self.state = TaskState.RUNNING
        elif self.state is TaskState.RUNNING and event in {
            "child_failed",
            "child_timeout",
            "schema_invalid",
        }:
            self.attempt += 1
            self.state = TaskState.ESCALATED if self.attempt > 3 else TaskState.RUNNING
        else:
            raise InvalidTransition(f"invalid task transition: {self.state}/{event}")
        return self.state
