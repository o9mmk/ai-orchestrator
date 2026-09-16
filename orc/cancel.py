"""M8 human cancel with process-group stop evidence and retention."""

from __future__ import annotations

from dataclasses import dataclass

from orc.process_ledger import ProcessLedger
from orc.state_machine import RunState
from orc.store import RunStateStore


@dataclass(frozen=True)
class CancelOutcome:
    """Bounded evidence returned after all ledgered groups are terminal."""

    state: str
    terminated_children: int


class CancelService:
    """Move a nonterminal run to CANCELLED without deleting audit material."""

    def __init__(
        self,
        store: RunStateStore,
        *,
        term_grace_seconds: float = 5.0,
        poll_interval: float = 0.05,
    ) -> None:
        self.store = store
        self.ledger = ProcessLedger(
            store,
            term_grace_seconds=term_grace_seconds,
            poll_interval=poll_interval,
        )

    def cancel(self) -> CancelOutcome:
        """Stop every child, mark active tasks ABORTED, then finalize CANCELLED."""
        current = RunState(self.store.read_manifest()["state"])
        if current.terminal:
            raise ValueError(f"terminal run cannot be cancelled: {current.value}")
        self.store.transition("cancel_requested", reason="cancel_requested")
        evidence = self.ledger.cancel_running()
        for task_id, task in self.store.read_tasks().items():
            if task["state"] in {"DONE", "ESCALATED", "ABORTED"}:
                continue
            updated = task.copy()
            updated["state"] = "ABORTED"
            self.store.record_task_state(task_id, updated)
        state = self.store.transition("children_stopped", reason="all_children_stopped")
        return CancelOutcome(state.value, len(evidence))
