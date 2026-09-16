"""M4 result.json schema boundary tests."""

from copy import deepcopy

import pytest
from jsonschema import ValidationError

from orc.result_schema import validate_result


def valid_result() -> dict[str, object]:
    """Return one complete result.json document."""
    return {
        "task_id": "task-1",
        "role": "implementer",
        "attempt": 1,
        "claimed_status": "done",
        "summary": "implemented the requested change",
        "changed_files": ["src/change.py"],
        "self_check": {"command": "pytest -q", "exit_code": 0},
        "references": ["FINAL_DESIGN.md#9"],
        "truncated": False,
        "context_requests_used": 0,
    }


def test_result_schema_accepts_complete_bounded_result() -> None:
    """A complete result with optional fields passes validation."""
    result = valid_result()

    validate_result(
        result,
        expected_task_id="task-1",
        expected_role="implementer",
        expected_attempt=1,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role", "planner"),
        ("attempt", 0),
        ("summary", "x" * 2001),
        ("changed_files", ["../parent.txt"]),
        ("changed_files", ["/absolute.txt"]),
        ("context_requests_used", 3),
    ],
)
def test_result_schema_rejects_type_and_range_violations(
    field: str,
    value: object,
) -> None:
    """Role, attempt, summary, path, and request bounds fail loudly."""
    result = deepcopy(valid_result())
    result[field] = value

    with pytest.raises(ValidationError):
        validate_result(
            result,
            expected_task_id="task-1",
            expected_role="implementer",
            expected_attempt=1,
        )

@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "other-task"),
        ("role", "researcher"),
        ("attempt", 2),
    ],
)
def test_result_identity_must_match_the_attempt(
    field: str,
    value: object,
) -> None:
    """A child cannot relabel its task, role, or attempt."""
    result = deepcopy(valid_result())
    result[field] = value

    with pytest.raises(ValidationError, match="does not match"):
        validate_result(
            result,
            expected_task_id="task-1",
            expected_role="implementer",
            expected_attempt=1,
        )
