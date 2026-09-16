"""Manifest-bound monotonic budget accounting and halt enforcement."""

from __future__ import annotations

import time
from collections.abc import Callable

from orc.budget_models import BudgetLimitReached, BudgetSnapshot, validate_cap_order
from orc.process_ledger import ProcessLedger
from orc.process_schema import TerminationEvidence
from orc.state_machine import RunState
from orc.store import RunStateStore
from orc.usage import BudgetSource, UsageValue

_SOURCE_ORDER = {
    BudgetSource.MEASURED: 0,
    BudgetSource.BYTES_PROXY: 1,
    BudgetSource.COUNT_PROXY: 2,
}

__all__ = ["BudgetLimitReached", "BudgetMeter", "BudgetSnapshot"]


class BudgetMeter:
    """Update counters monotonically and check every cap before spawn."""

    def __init__(self, store: RunStateStore, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.store = store
        self.clock = clock
        manifest = store.read_manifest()
        self.caps = manifest["caps"].copy()
        validate_cap_order(self.caps)
        persisted = store.read_budget()
        self._tokens_used = int(persisted["tokens_used"])
        self._source = BudgetSource(persisted["budget_source"])
        self._child_invocations = int(persisted["child_invocations"])
        self._manager_calls = int(persisted["manager_calls"])
        self._active_seconds = float(persisted["active_seconds"])
        self._calendar_seconds = float(persisted["calendar_seconds"])
        self._halt_reason = persisted["halt_reason"]
        self._origin = self.clock()
        self._active_started: float | None = None
        self._activities: set[str] = set()

    def snapshot(self) -> BudgetSnapshot:
        """Return current counters, including elapsed active/calendar time."""
        now = self.clock()
        active = self._active_seconds
        if self._active_started is not None:
            active += now - self._active_started
        calendar = self._calendar_seconds + now - self._origin
        soft = self._soft_reached(active)
        hard = self._hard_reached(active)
        return BudgetSnapshot(
            self._tokens_used,
            self._source,
            self._child_invocations,
            self._manager_calls,
            active,
            calendar,
            soft,
            hard,
            self._halt_reason,
        )

    def reserve_child(self) -> BudgetSnapshot:
        """Atomically reserve one non-Manager child invocation before spawn."""
        self._ensure_launch_allowed()
        self._child_invocations += 1
        return self._persist()

    def reserve_planner(self) -> BudgetSnapshot:
        """Atomically reserve one Planner child and one Manager LLM call."""
        self._ensure_launch_allowed()
        self._child_invocations += 1
        self._manager_calls += 1
        return self._persist()

    def add_usage(self, usage: UsageValue) -> BudgetSnapshot:
        """Add usage without ever upgrading a proxy aggregate to measured."""
        self._tokens_used += usage.tokens
        if _SOURCE_ORDER[usage.source] > _SOURCE_ORDER[self._source]:
            self._source = usage.source
        return self._persist()

    def begin_activity(self, activity_id: str) -> BudgetSnapshot:
        """Start active execution; concurrent work counts wall time only once."""
        if not activity_id or activity_id in self._activities:
            raise ValueError("activity_id must be new and non-empty")
        if not self._activities:
            self._active_started = self.clock()
        self._activities.add(activity_id)
        return self._persist()

    def end_activity(self, activity_id: str) -> BudgetSnapshot:
        """Finish an active interval and persist elapsed execution time."""
        if activity_id not in self._activities:
            raise ValueError("unknown budget activity_id")
        self._roll_time()
        self._activities.remove(activity_id)
        if not self._activities:
            self._active_started = None
        return self._persist(roll_time=False)

    def bound_timeout(self, configured_seconds: float) -> float:
        """Bound one OS timeout by the remaining active hard cap."""
        if configured_seconds <= 0:
            raise ValueError("configured timeout must be positive")
        remaining = float(self.caps["active_seconds_hard"]) - self.snapshot().active_seconds
        if remaining <= 0:
            self._deny("budget_hard", hard=True)
        return min(configured_seconds, remaining)

    def enforce_soft(self, *, pending_tasks: bool, running_children: int) -> bool:
        """Halt pending work only after every already-running child finishes."""
        if running_children < 0:
            raise ValueError("running_children must be non-negative")
        current = self.snapshot()
        if current.hard_reached or not current.soft_reached:
            return False
        if not pending_tasks or running_children:
            return False
        self._halt_reason = "budget_soft"
        snapshot = self._persist()
        self.store.append_event(
            "budget_soft_halt",
            "manager",
            {"reason": "budget_soft", "budget": snapshot.to_checkpoint()},
        )
        self._transition_halt(hard=False)
        return True

    def enforce_hard(self, ledger: ProcessLedger) -> tuple[TerminationEvidence, ...]:
        """Kill every ledgered RUNNING group and record a hard HALT."""
        if not self.snapshot().hard_reached:
            raise ValueError("hard budget has not been reached")
        self._halt_reason = "budget_hard"
        snapshot = self._persist()
        evidence = ledger.terminate_running(reason="budget_hard")
        self.store.append_event(
            "budget_hard_halt",
            "manager",
            {
                "reason": "budget_hard",
                "terminated_children": len(evidence),
                "budget": snapshot.to_checkpoint(),
            },
        )
        self._transition_halt(hard=True)
        return evidence

    def halt_before_spawn(self, limit: BudgetLimitReached) -> None:
        """Persist a pre-spawn denial and transition the active run to HALTED."""
        self._halt_reason = limit.reason
        snapshot = self._persist()
        self.store.append_event(
            "budget_launch_halt",
            "manager",
            {"reason": limit.reason, "hard": limit.hard, "budget": snapshot.to_checkpoint()},
        )
        self._transition_halt(hard=limit.hard)

    def _ensure_launch_allowed(self) -> None:
        current = self.snapshot()
        if current.hard_reached:
            self._deny("budget_hard", hard=True)
        if current.soft_reached:
            self._deny("budget_soft", hard=False)

    def _deny(self, reason: str, *, hard: bool) -> None:
        self._halt_reason = reason
        self._persist()
        raise BudgetLimitReached(reason, hard=hard)

    def _persist(self, *, roll_time: bool = True) -> BudgetSnapshot:
        if roll_time:
            self._roll_time()
        snapshot = self.snapshot()
        self.store.record_budget(snapshot.to_checkpoint())
        return snapshot

    def _roll_time(self) -> None:
        now = self.clock()
        self._calendar_seconds += now - self._origin
        self._origin = now
        if self._active_started is not None:
            self._active_seconds += now - self._active_started
            self._active_started = now

    def _soft_reached(self, active_seconds: float) -> bool:
        return any(
            (
                self._tokens_used >= self.caps["tokens_soft"],
                self._child_invocations >= self.caps["child_invocations_soft"],
                self._manager_calls >= self.caps["manager_calls_soft"],
                active_seconds >= self.caps["active_seconds_soft"],
            )
        )

    def _hard_reached(self, active_seconds: float) -> bool:
        return any(
            (
                self._tokens_used >= self.caps["tokens_hard"],
                self._child_invocations >= self.caps["child_invocations_hard"],
                self._manager_calls >= self.caps["manager_calls_hard"],
                active_seconds >= self.caps["active_seconds_hard"],
            )
        )

    def _transition_halt(self, *, hard: bool) -> None:
        state = RunState(self.store.read_manifest()["state"])
        if state is RunState.PLANNING:
            event = "budget_hard" if hard else "budget_soft"
        elif state is RunState.RUNNING:
            event = "budget_hard" if hard else "budget_soft_remaining"
        else:
            raise ValueError(f"budget halt is invalid from state: {state.value}")
        self.store.transition(event, reason=self._halt_reason)
