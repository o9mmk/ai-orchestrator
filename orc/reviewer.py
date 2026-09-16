"""Finite Claude-to-Codex-to-none reviewer fallback."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from orc.artifact_ingest import ArtifactIngestor
from orc.budget import BudgetLimitReached
from orc.errors import (
    ChildExecutionError,
    ClaudeCapabilityError,
    CodexCapabilityError,
    DlpBoundaryError,
    ReviewExecutionError,
)
from orc.review_bundle import ReviewBundle
from orc.review_runner import ReviewRunner
from orc.store import RunStateStore
from orc.worktree import Worktree


@dataclass(frozen=True)
class ReviewerOutcome:
    """Fallback result consumed by completion/reporting."""

    reviewed_by: str
    report: dict[str, Any] | None
    external_review_skipped: bool
    reason: str


class ReviewerService:
    """Try each reviewer at most once and make none explicit."""

    def __init__(
        self,
        store: RunStateStore,
        dlp_ingestor: ArtifactIngestor,
        *,
        claude_runner: ReviewRunner | None,
        codex_runner: ReviewRunner | None,
    ) -> None:
        self.store = store
        self.dlp_ingestor = dlp_ingestor
        self.claude_runner = claude_runner
        self.codex_runner = codex_runner

    def review(
        self,
        worktree: Worktree,
        *,
        task_id: str,
        attempt: int,
        bundle: ReviewBundle,
    ) -> ReviewerOutcome:
        """DLP/size gate the bundle, then execute the finite fallback chain."""
        if not bundle.within_limit:
            return self._none(task_id, "review_bundle_limit")
        assessment = self.dlp_ingestor.assess_manager_payload(
            "review_bundle", secrets.token_hex(16), bundle.payload
        )
        if not assessment.result.clean or assessment.clearance is None:
            return self._none(task_id, "review_bundle_dlp_blocked")
        last_reason = "reviewer_unavailable"
        for runner in (self.claude_runner, self.codex_runner):
            if runner is None:
                continue
            try:
                outcome = runner.run(
                    worktree,
                    task_id=task_id,
                    attempt=attempt,
                    bundle=bundle,
                )
            except BudgetLimitReached:
                return self._none(task_id, "review_budget_blocked")
            except (
                ChildExecutionError,
                ClaudeCapabilityError,
                CodexCapabilityError,
                DlpBoundaryError,
                ReviewExecutionError,
            ) as error:
                last_reason = _safe_failure_reason(error)
                continue
            if outcome.status == "VALIDATED" and outcome.report is not None:
                return ReviewerOutcome(
                    outcome.reviewed_by,
                    outcome.report,
                    False,
                    "review_validated",
                )
            last_reason = outcome.reason
        return self._none(task_id, last_reason)

    def _none(self, task_id: str, reason: str) -> ReviewerOutcome:
        self.store.append_event(
            "external_review_skipped",
            "manager",
            {"reviewed_by": "none", "reason": reason},
            task_id=task_id,
        )
        self.store.write_checkpoint(run_state=self.store.read_manifest()["state"])
        return ReviewerOutcome("none", None, True, reason)


def _safe_failure_reason(error: Exception) -> str:
    if isinstance(error, ClaudeCapabilityError):
        return "claude_unavailable"
    if isinstance(error, CodexCapabilityError):
        return "codex_unavailable"
    if isinstance(error, DlpBoundaryError):
        return "review_dlp_unavailable"
    return "review_execution_failed"
