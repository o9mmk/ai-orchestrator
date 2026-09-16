"""設計書§9 plan.json schema。"""

from __future__ import annotations

from typing import Any

from jsonschema import ValidationError, validate

SIZE_VALUES = ["S", "M", "L", "XL"]

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tasks", "planner_size", "deterministic_size", "final_size"],
    "properties": {
        "tasks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "task_id",
                    "role",
                    "objective",
                    "path_scope",
                    "acceptance",
                    "depends_on",
                    "size_estimate",
                    "scope_confidence",
                ],
                "properties": {
                    "task_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "role": {"enum": ["researcher", "implementer", "reviewer", "verifier"]},
                    "objective": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "path_scope": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 1000,
                        "items": {"type": "string", "minLength": 1, "maxLength": 1024},
                    },
                    # 読むだけのpath。書き込み許可には決して昇格しない。
                    "read_scope": {
                        "type": "array",
                        "maxItems": 1000,
                        "items": {"type": "string", "minLength": 1, "maxLength": 1024},
                    },
                    "acceptance": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 100,
                        "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                    },
                    "depends_on": {
                        "type": "array",
                        "maxItems": 100,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                    "size_estimate": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "estimated_files",
                            "estimated_diff_lines",
                            "estimated_invocations",
                        ],
                        "properties": {
                            "estimated_files": {"type": "integer", "minimum": 0},
                            "estimated_diff_lines": {"type": "integer", "minimum": 0},
                            "estimated_invocations": {"type": "integer", "minimum": 0},
                        },
                    },
                    "scope_confidence": {"enum": ["high", "medium", "low"]},
                    "commands": {
                        "type": "array",
                        "maxItems": 100,
                        "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    },
                },
            },
        },
        "planner_size": {"enum": SIZE_VALUES},
        "deterministic_size": {"enum": SIZE_VALUES},
        "final_size": {"enum": SIZE_VALUES},
    },
}


def validate_plan(data: dict[str, Any]) -> None:
    """planの必須fieldと型を検証する。"""
    validate(instance=data, schema=PLAN_SCHEMA)
    tasks = data["tasks"]
    task_ids = [task["task_id"] for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValidationError("task_id must be unique")
    known = set(task_ids)
    for task in tasks:
        dependencies = set(task["depends_on"])
        if task["task_id"] in dependencies:
            raise ValidationError(f"task cannot depend on itself: {task['task_id']}")
        unknown = dependencies - known
        if unknown:
            raise ValidationError(
                f"unknown task dependency for {task['task_id']}: {','.join(sorted(unknown))}"
            )
