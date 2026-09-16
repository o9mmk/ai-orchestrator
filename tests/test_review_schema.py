"""M7 review.json schema tests."""

from copy import deepcopy

import pytest
from jsonschema import ValidationError

from orc.review_schema import validate_review


def valid_review(*, reviewed_by: str = "claude") -> dict:
    """Return one bounded schema-valid review report."""
    return {
        "verdict": "approve",
        "findings": [],
        "reviewed_by": reviewed_by,
        "input_digest": "a" * 64,
    }


def test_review_schema_accepts_bounded_report() -> None:
    report = valid_review()

    validate_review(
        report,
        expected_reviewer="claude",
        expected_input_digest="a" * 64,
    )


def test_review_schema_rejects_reviewer_or_input_digest_spoofing() -> None:
    report = valid_review(reviewed_by="codex")

    with pytest.raises(ValidationError, match="reviewed_by"):
        validate_review(
            report,
            expected_reviewer="claude",
            expected_input_digest="a" * 64,
        )

    report = valid_review()
    report["input_digest"] = "b" * 64
    with pytest.raises(ValidationError, match="input_digest"):
        validate_review(
            report,
            expected_reviewer="claude",
            expected_input_digest="a" * 64,
        )


def test_review_schema_rejects_more_than_ten_findings_and_extra_fields() -> None:
    finding = {
        "severity": "high",
        "location": "src/app.py:10",
        "evidence": "bounded evidence",
        "expected_behavior": "reject unsafe input",
    }
    report = valid_review()
    report["verdict"] = "request_changes"
    report["findings"] = [deepcopy(finding) for _ in range(11)]

    with pytest.raises(ValidationError):
        validate_review(
            report,
            expected_reviewer="claude",
            expected_input_digest="a" * 64,
        )

    report = valid_review()
    report["unexpected"] = "not allowed"
    with pytest.raises(ValidationError):
        validate_review(
            report,
            expected_reviewer="claude",
            expected_input_digest="a" * 64,
        )
