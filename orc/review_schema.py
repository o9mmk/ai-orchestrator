"""FINAL_DESIGN.md §9 review.json schema and identity binding."""

from __future__ import annotations

from typing import Any, cast

from jsonschema import ValidationError, validate

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "findings", "reviewed_by", "input_digest"],
    "properties": {
        "verdict": {"enum": ["approve", "request_changes"]},
        "findings": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "severity",
                    "location",
                    "evidence",
                    "expected_behavior",
                ],
                "properties": {
                    "severity": {
                        "enum": ["blocker", "high", "medium", "low", "info"]
                    },
                    "location": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "evidence": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "expected_behavior": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4000,
                    },
                },
            },
        },
        "reviewed_by": {"enum": ["claude", "codex", "none"]},
        "input_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
}


def validate_review(
    data: Any,
    *,
    expected_reviewer: str | None = None,
    expected_input_digest: str | None = None,
) -> dict[str, Any]:
    """Validate bounded findings and bind a report to the actual reviewer/input."""
    validate(instance=data, schema=REVIEW_SCHEMA)
    if data["verdict"] == "request_changes" and not data["findings"]:
        raise ValidationError("request_changes review must contain a finding")
    if expected_reviewer is not None and data["reviewed_by"] != expected_reviewer:
        raise ValidationError("reviewed_by does not match the executing adapter")
    if expected_input_digest is not None and data["input_digest"] != expected_input_digest:
        raise ValidationError("input_digest does not match the sent review bundle")
    return cast(dict[str, Any], data)
