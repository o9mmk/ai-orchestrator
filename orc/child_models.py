"""Bounded public models for M4 child execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChildAttemptOutcome:
    """Manager-facing outcome; raw child and invalid JSON content is excluded."""

    status: str
    task_id: str
    role: str
    attempt: int
    exit_code: int | None
    timed_out: bool
    termination_signal: str | None
    forced_kill: bool
    attempt_consumed: bool
    task_state: str
    manager_result: dict[str, Any] | None
    stdout_digest: str | None
    stderr_digest: str | None
    pid: int
    pgid: int
    dlp_status: str = "CLEAN"
