"""Design §9 result.json schema and attempt identity checks."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, cast

from jsonschema import ValidationError, validate

ROLE_VALUES = ["researcher", "implementer", "reviewer"]

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "task_id",
        "role",
        "attempt",
        "claimed_status",
        "summary",
        "changed_files",
        "truncated",
        "context_requests_used",
    ],
    "properties": {
        "task_id": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$",
        },
        "role": {"enum": ROLE_VALUES},
        "attempt": {"type": "integer", "minimum": 1},
        "claimed_status": {"type": "string", "minLength": 1, "maxLength": 128},
        "summary": {"type": "string", "maxLength": 2000},
        "changed_files": {
            "type": "array",
            "maxItems": 1000,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 1024},
        },
        "self_check": {
            "type": "object",
            "additionalProperties": False,
            "required": ["command", "exit_code"],
            "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 4096},
                "exit_code": {"type": "integer", "minimum": -255, "maximum": 255},
            },
        },
        "references": {
            "type": "array",
            "maxItems": 100,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 1024},
        },
        "truncated": {"type": "boolean"},
        "context_requests_used": {"type": "integer", "minimum": 0, "maximum": 2},
    },
}


def validate_result(
    data: Any,
    *,
    expected_task_id: str,
    expected_role: str,
    expected_attempt: int,
) -> dict[str, Any]:
    """Validate bounded fields and bind the result to its requested attempt."""
    validate(instance=data, schema=RESULT_SCHEMA)
    if data["task_id"] != expected_task_id:
        raise ValidationError("result task_id does not match requested attempt")
    if data["role"] != expected_role:
        raise ValidationError("result role does not match requested attempt")
    if data["attempt"] != expected_attempt:
        raise ValidationError("result attempt does not match requested attempt")
    for changed_file in data["changed_files"]:
        if not _safe_relative_file(changed_file):
            raise ValidationError("result changed_files contains an unsafe path")
    return cast(dict[str, Any], data)


def _safe_relative_file(value: str) -> bool:
    """Accept canonical repo-relative POSIX file paths only."""
    if "\0" in value or "\\" in value or value.startswith("/"):
        return False
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    return not PurePosixPath(value).is_absolute()
