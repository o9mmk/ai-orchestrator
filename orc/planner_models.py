"""Bounded Planner public and per-attempt models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlannerOutcome:
    """Manager-facing Planner result with no raw or invalid child body."""

    status: str
    attempts: int
    plan: dict[str, Any] | None
    final_size: str | None
    reason: str | None
    prompt_digest: str | None


@dataclass(frozen=True)
class PlannerAttemptResult:
    """One attempt reduced to a valid plan or bounded failure evidence."""

    plan: dict[str, Any] | None
    reason: str | None
    content_digest: str
    stdout_digest: str | None
    stderr_digest: str | None
