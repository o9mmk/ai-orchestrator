"""M7 evidence-only semantic completion tests."""

from copy import deepcopy

from orc.completion import CompletionAction, CompletionEvaluator
from tests.test_review_schema import valid_review


def verify_report(classification: str = "PASS") -> dict:
    """Return a minimal completion-facing verify report."""
    return {
        "gates": [
            {
                "name": "pytest",
                "command": ["pytest", "-q"],
                "exit_code": 0,
                "baseline_result": {"exit_code": 0, "timed_out": False},
                "candidate_result": {
                    "attempts": [{"exit_code": 0, "timed_out": False}]
                },
                "classification": classification,
                "log_digest": "b" * 64,
                "limits": {"verified": True, "unsupported": []},
                "violation": None,
            }
        ],
        "scope_check": {"patch_files": ["app.py"], "in_scope": True},
        "secret_scan": {
            "tool": "fake-gitleaks",
            "findings_count": 0,
            "quarantined": False,
        },
        "sandbox_profile": "test",
    }


def test_at6_claimed_done_cannot_override_failed_verification() -> None:
    """The evaluator has no claimed_status input and rejects regression evidence."""
    decision = CompletionEvaluator().evaluate(
        verify_report("REGRESSION"),
        valid_review(),
        review_cycles=0,
    )

    assert decision.action is CompletionAction.RETRY
    assert decision.reason == "verification_failed"


def test_approved_review_and_clean_verification_are_done() -> None:
    decision = CompletionEvaluator().evaluate(
        verify_report(),
        valid_review(),
        review_cycles=0,
    )

    assert decision.action is CompletionAction.DONE


def test_none_reviewer_and_baseline_failure_require_human_approval() -> None:
    evaluator = CompletionEvaluator()

    no_reviewer = evaluator.evaluate(verify_report(), None, review_cycles=0)
    baseline_failed = evaluator.evaluate(
        verify_report("BASELINE_FAILED"),
        valid_review(),
        review_cycles=0,
    )

    assert no_reviewer.action is CompletionAction.AWAITING_APPROVAL
    assert no_reviewer.reason == "independent_review_unavailable"
    assert baseline_failed.action is CompletionAction.AWAITING_APPROVAL
    assert baseline_failed.reason == "baseline_failed"


def test_at16_second_request_changes_escalates_without_third_fix_cycle() -> None:
    report = deepcopy(valid_review())
    report["verdict"] = "request_changes"
    report["findings"] = [
        {
            "severity": "high",
            "location": "app.py:1",
            "evidence": "unsafe behavior",
            "expected_behavior": "reject the input",
        }
    ]
    evaluator = CompletionEvaluator(review_cycles_hard=2)

    first = evaluator.evaluate(verify_report(), report, review_cycles=0)
    second = evaluator.evaluate(verify_report(), report, review_cycles=1)

    assert first.action is CompletionAction.FIXING
    assert first.review_cycles == 1
    assert second.action is CompletionAction.ESCALATED
    assert second.review_cycles == 2
