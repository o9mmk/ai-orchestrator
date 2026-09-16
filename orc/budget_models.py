"""Public budget snapshot and limit models."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from orc.usage import BudgetSource

DEFAULT_CAPS: dict[str, Any] = {
    "tokens_soft": 2_000_000,
    "tokens_hard": 4_000_000,
    "child_invocations_soft": 15,
    "child_invocations_hard": 20,
    "concurrency_hard": 2,
    "task_attempts_soft": 2,
    "task_attempts_hard": 3,
    "review_cycles_hard": 2,
    "timeouts_seconds": {
        "research": 600,
        "implement": 1200,
        "review": 600,
        "verify": 900,
    },
    "active_seconds_soft": 5400,
    "active_seconds_hard": 7200,
    "manager_calls_soft": 5,
    "manager_calls_hard": 8,
    "manager_input_tokens_hard": 20_000,
    "result_summary_soft_chars": 2_000,
    "result_summary_hard_chars": 4_000,
    "review_findings_hard": 10,
}


def default_budget_caps() -> dict[str, Any]:
    """Return an isolated copy of FINAL_DESIGN §7.1 defaults."""
    return copy.deepcopy(DEFAULT_CAPS)


class BudgetLimitReached(RuntimeError):
    """A soft or hard pre-spawn budget gate denied a new invocation."""

    def __init__(self, reason: str, *, hard: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.hard = hard


@dataclass(frozen=True)
class BudgetSnapshot:
    """Manager-facing budget evidence without raw child content."""

    tokens_used: int
    budget_source: BudgetSource
    child_invocations: int
    manager_calls: int
    active_seconds: float
    calendar_seconds: float
    soft_reached: bool
    hard_reached: bool
    halt_reason: str | None

    def to_checkpoint(self) -> dict[str, Any]:
        """Return the schema-bound checkpoint/event representation."""
        return {
            "tokens_used": self.tokens_used,
            "budget_source": self.budget_source.value,
            "invocations": self.child_invocations,
            "child_invocations": self.child_invocations,
            "manager_calls": self.manager_calls,
            "active_seconds": self.active_seconds,
            "calendar_seconds": self.calendar_seconds,
            "soft_reached": self.soft_reached,
            "hard_reached": self.hard_reached,
            "halt_reason": self.halt_reason,
        }


def validate_cap_order(caps: Mapping[str, Any]) -> None:
    """Reject any run whose configured soft cap exceeds its hard cap."""
    pairs = (
        ("tokens_soft", "tokens_hard", "tokens"),
        ("child_invocations_soft", "child_invocations_hard", "child_invocations"),
        ("task_attempts_soft", "task_attempts_hard", "task_attempts"),
        ("active_seconds_soft", "active_seconds_hard", "active_seconds"),
        ("manager_calls_soft", "manager_calls_hard", "manager_calls"),
        ("result_summary_soft_chars", "result_summary_hard_chars", "result_summary"),
    )
    for soft, hard, label in pairs:
        if caps[soft] > caps[hard]:
            raise ValueError(f"budget soft cap exceeds hard cap: {label}")


def validate_budget_caps(caps: Mapping[str, Any]) -> None:
    """全capを正の非bool整数として、既定shapeとsoft/hard順序で検証する。"""
    if set(caps) != set(DEFAULT_CAPS):
        raise ValueError("budget caps must use the complete known key set")
    timeouts = caps.get("timeouts_seconds")
    expected_timeouts = DEFAULT_CAPS["timeouts_seconds"]
    if not isinstance(timeouts, Mapping) or set(timeouts) != set(expected_timeouts):
        raise ValueError("timeouts_seconds must use the complete known key set")
    scalar_values = {key: value for key, value in caps.items() if key != "timeouts_seconds"}
    scalar_values.update({f"timeouts_seconds.{key}": value for key, value in timeouts.items()})
    for key, value in scalar_values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"budget cap must be a positive integer: {key}")
    validate_cap_order(caps)
