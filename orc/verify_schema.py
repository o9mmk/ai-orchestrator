"""設計書§9 verify.json schema。"""

from __future__ import annotations

from typing import Any

from jsonschema import validate

RUN_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["exit_code", "timed_out"],
    "properties": {
        "exit_code": {"type": "integer"},
        "timed_out": {"type": "boolean"},
    },
}

VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["gates", "scope_check", "secret_scan", "sandbox_profile"],
    "properties": {
        "gates": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "name",
                    "command",
                    "exit_code",
                    "baseline_result",
                    "candidate_result",
                    "classification",
                    "log_digest",
                ],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    "command": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 256,
                        "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    },
                    "exit_code": {"type": "integer"},
                    "baseline_result": RUN_RESULT_SCHEMA,
                    "candidate_result": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["attempts"],
                        "properties": {
                            "attempts": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 2,
                                "items": RUN_RESULT_SCHEMA,
                            }
                        },
                    },
                    "classification": {
                        "enum": [
                            "PASS",
                            "REGRESSION",
                            "BASELINE_FAILED",
                            "FIXED_EXISTING_FAILURE",
                            "FLAKY",
                            "INCONCLUSIVE",
                        ]
                    },
                    "log_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
            },
        },
        "scope_check": {
            "type": "object",
            "additionalProperties": False,
            "required": ["patch_files", "in_scope"],
            "properties": {
                "patch_files": {
                    "type": "array",
                    "maxItems": 1000,
                    "items": {"type": "string", "minLength": 1, "maxLength": 1024},
                },
                "in_scope": {"type": "boolean"},
            },
        },
        "secret_scan": {
            "type": "object",
            "additionalProperties": False,
            "required": ["tool", "findings_count", "quarantined"],
            "properties": {
                "tool": {"type": "string", "minLength": 1, "maxLength": 128},
                "findings_count": {"type": "integer", "minimum": 0},
                "quarantined": {"type": "boolean"},
            },
        },
        "sandbox_profile": {"type": "string", "minLength": 1, "maxLength": 256},
    },
}


def validate_verify_report(data: dict[str, Any]) -> None:
    """verify reportの全必須fieldを検証する。"""
    validate(instance=data, schema=VERIFY_SCHEMA)
