"""Evidence-only M7 semantic completion decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from orc.review_schema import validate_review
from orc.verify_schema import validate_verify_report


class CompletionAction(StrEnum):
    """Finite actions available after verify/review evidence."""

    DONE = "DONE"
    RETRY = "RETRY"
    FIXING = "FIXING"
    ESCALATED = "ESCALATED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"


@dataclass(frozen=True)
class CompletionDecision:
    """Bounded completion outcome; claimed_status is intentionally absent."""

    action: CompletionAction
    reason: str
    review_cycles: int


class CompletionEvaluator:
    """Decide completion from validated machine/reviewer evidence only."""

    def __init__(self, *, review_cycles_hard: int = 2) -> None:
        if review_cycles_hard < 1:
            raise ValueError("review_cycles_hard must be positive")
        self.review_cycles_hard = review_cycles_hard

    def evaluate(
        self,
        verify_report: dict[str, Any],
        review_report: dict[str, Any] | None,
        *,
        review_cycles: int,
    ) -> CompletionDecision:
        """Return the next finite action without accepting a child completion claim."""
        if review_cycles < 0:
            raise ValueError("review_cycles must be non-negative")
        validate_verify_report(verify_report)
        failure = _verification_failure(verify_report)
        if failure is not None:
            return CompletionDecision(CompletionAction.RETRY, failure, review_cycles)
        classifications = {
            gate["classification"] for gate in verify_report["gates"]
        }
        if "BASELINE_FAILED" in classifications:
            return CompletionDecision(
                CompletionAction.AWAITING_APPROVAL,
                "baseline_failed",
                review_cycles,
            )
        if review_report is None:
            return CompletionDecision(
                CompletionAction.AWAITING_APPROVAL,
                "independent_review_unavailable",
                review_cycles,
            )
        validate_review(review_report)
        if review_report["reviewed_by"] == "none":
            return CompletionDecision(
                CompletionAction.AWAITING_APPROVAL,
                "independent_review_unavailable",
                review_cycles,
            )
        if review_report["verdict"] == "approve":
            return CompletionDecision(CompletionAction.DONE, "evidence_complete", review_cycles)
        next_cycle = review_cycles + 1
        if next_cycle >= self.review_cycles_hard:
            return CompletionDecision(
                CompletionAction.ESCALATED,
                "review_cycle_cap",
                next_cycle,
            )
        return CompletionDecision(
            CompletionAction.FIXING,
            "review_requested_changes",
            next_cycle,
        )


def _verification_failure(report: dict[str, Any]) -> str | None:
    if not report["scope_check"]["in_scope"]:
        return "verification_failed"
    secret_scan = report["secret_scan"]
    if secret_scan["findings_count"] or secret_scan["quarantined"]:
        return "verification_failed"
    blocked = {"REGRESSION", "FLAKY", "INCONCLUSIVE"}
    if any(gate["classification"] in blocked for gate in report["gates"]):
        return "verification_failed"
    return None
