"""worktrees.json schema and bounded recovery evidence model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

STATUS_VALUES = [
    "RUNNING",
    "EXITED",
    "TIMED_OUT_TERM",
    "TIMED_OUT_KILL",
    "ABORTED",
    "ABORTED_TERM",
    "ABORTED_KILL",
    "RECOVERED_TERM",
    "RECOVERED_KILL",
    "BUDGET_TERM",
    "BUDGET_KILL",
    "CANCEL_TERM",
    "CANCEL_KILL",
    "EXITED_UNRECORDED",
]

LEDGER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "processes"],
    "properties": {
        "version": {"const": 1},
        "processes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "task_id",
                    "attempt",
                    "pid",
                    "pgid",
                    "started_at",
                    "worktree",
                    "status",
                ],
                "properties": {
                    "task_id": {"type": "string", "minLength": 1},
                    "attempt": {"type": "integer", "minimum": 1},
                    "pid": {"type": "integer", "minimum": 1},
                    "pgid": {"type": "integer", "minimum": 1},
                    "started_at": {"type": "string", "minLength": 1},
                    "worktree": {"type": "string", "minLength": 1},
                    "status": {"enum": STATUS_VALUES},
                    "exit_code": {"type": ["integer", "null"]},
                    "timed_out": {"type": "boolean"},
                    "signal": {"type": ["string", "null"]},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class RecoveryResult:
    """Bounded evidence for one startup orphan decision."""

    task_id: str
    attempt: int
    pid: int
    pgid: int
    signal: str | None
    status: str


@dataclass(frozen=True)
class TerminationEvidence:
    """Hard-budget termination evidence for one ledgered group."""

    task_id: str
    attempt: int
    pid: int
    pgid: int
    signal: str | None
    status: str
    exit_code: int | None
