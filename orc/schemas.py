"""設計書§9 JSON artifactの必須schema。"""

from __future__ import annotations

from typing import Any

from jsonschema import validate

RUN_STATES = [
    "INIT",
    "PREFLIGHT1",
    "PLANNING",
    "PREFLIGHT2",
    "AWAITING_START_APPROVAL",
    "RUNNING",
    "INTEGRATING",
    "AWAITING_APPROVAL",
    "CANCELLING",
    "REFUSED",
    "FAILED",
    "HALTED",
    "CANCELLED",
    "COMPLETED",
]

CAPS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "tokens_soft",
        "tokens_hard",
        "child_invocations_soft",
        "child_invocations_hard",
        "concurrency_hard",
        "task_attempts_soft",
        "task_attempts_hard",
        "review_cycles_hard",
        "timeouts_seconds",
        "active_seconds_soft",
        "active_seconds_hard",
        "manager_calls_soft",
        "manager_calls_hard",
        "manager_input_tokens_hard",
        "result_summary_soft_chars",
        "result_summary_hard_chars",
        "review_findings_hard",
    ],
    "properties": {
        "tokens_soft": {"type": "integer", "minimum": 1},
        "tokens_hard": {"type": "integer", "minimum": 1},
        "child_invocations_soft": {"type": "integer", "minimum": 1},
        "child_invocations_hard": {"type": "integer", "minimum": 1},
        "concurrency_hard": {"type": "integer", "minimum": 1},
        "task_attempts_soft": {"type": "integer", "minimum": 1},
        "task_attempts_hard": {"type": "integer", "minimum": 1},
        "review_cycles_hard": {"type": "integer", "minimum": 1},
        "timeouts_seconds": {
            "type": "object",
            "additionalProperties": False,
            "required": ["research", "implement", "review", "verify"],
            "properties": {
                name: {"type": "integer", "minimum": 1}
                for name in ("research", "implement", "review", "verify")
            },
        },
        "active_seconds_soft": {"type": "integer", "minimum": 1},
        "active_seconds_hard": {"type": "integer", "minimum": 1},
        "manager_calls_soft": {"type": "integer", "minimum": 1},
        "manager_calls_hard": {"type": "integer", "minimum": 1},
        "manager_input_tokens_hard": {"type": "integer", "minimum": 1},
        "result_summary_soft_chars": {"type": "integer", "minimum": 1},
        "result_summary_hard_chars": {"type": "integer", "minimum": 1},
        "review_findings_hard": {"type": "integer", "minimum": 1},
    },
}

MANIFEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "run_id",
        "generation",
        "created_at",
        "goal",
        "acceptance_criteria",
        "forbidden",
        "authority_sources",
        "repo_path",
        "base_commit",
        "size",
        "caps",
        "budget_source",
        "reviewer_policy",
        "gates",
        "safety_policy_version",
        "state",
        "fencing_token",
    ],
    "properties": {
        "run_id": {"type": "string", "minLength": 1},
        "generation": {"type": "integer", "minimum": 1},
        "parent_run_id": {"type": ["string", "null"]},
        "resumed_from": {"type": ["integer", "null"], "minimum": 0},
        "created_at": {"type": "string", "minLength": 1},
        "goal": {"type": "string"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "forbidden": {"type": "array", "items": {"type": "string"}},
        "authority_sources": {"type": "array", "items": {"type": "string"}},
        "repo_path": {"type": "string", "minLength": 1},
        "base_commit": {"type": "string", "minLength": 1},
        "size": {"enum": ["S", "M", "L", "XL"]},
        "caps": CAPS_SCHEMA,
        "budget_source": {"enum": ["measured", "bytes_proxy", "count_proxy"]},
        "reviewer_policy": {"type": "string", "minLength": 1},
        "gates": {"type": "array", "items": {"type": "string"}},
        "safety_policy_version": {"type": "string", "minLength": 1},
        "state": {"enum": RUN_STATES},
        "fencing_token": {"type": "integer", "minimum": 0},
    },
}

EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ts", "seq", "prev_hash", "hash", "run_id", "type", "actor", "data"],
    "properties": {
        "ts": {"type": "string", "minLength": 1},
        "seq": {"type": "integer", "minimum": 1},
        "prev_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "run_id": {"type": "string", "minLength": 1},
        "task_id": {"type": "string", "minLength": 1},
        "type": {"type": "string", "minLength": 1},
        "actor": {"type": "string", "minLength": 1},
        "data": {"type": "object"},
    },
}

BUDGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "tokens_used",
        "budget_source",
        "invocations",
        "child_invocations",
        "manager_calls",
        "active_seconds",
        "calendar_seconds",
        "soft_reached",
        "hard_reached",
        "halt_reason",
    ],
    "properties": {
        "tokens_used": {"type": "integer", "minimum": 0},
        "budget_source": {"enum": ["measured", "bytes_proxy", "count_proxy"]},
        "invocations": {"type": "integer", "minimum": 0},
        "child_invocations": {"type": "integer", "minimum": 0},
        "manager_calls": {"type": "integer", "minimum": 0},
        "active_seconds": {"type": "number", "minimum": 0},
        "calendar_seconds": {"type": "number", "minimum": 0},
        "soft_reached": {"type": "boolean"},
        "hard_reached": {"type": "boolean"},
        "halt_reason": {"type": ["string", "null"]},
    },
}


CHECKPOINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "seq",
        "events_head_hash",
        "run_state",
        "tasks",
        "budget",
        "base_commit",
        "artifact_digests",
        "fencing_token",
    ],
    "properties": {
        "seq": {"type": "integer", "minimum": 0},
        "events_head_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "run_state": {"enum": RUN_STATES},
        "tasks": {"type": "object"},
        "budget": BUDGET_SCHEMA,
        "base_commit": {"type": "string", "minLength": 1},
        "artifact_digests": {
            "type": "object",
            "additionalProperties": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "fencing_token": {"type": "integer", "minimum": 0},
    },
}


def validate_manifest(data: dict[str, Any]) -> None:
    """manifestを§9 schemaで検証する。"""
    validate(instance=data, schema=MANIFEST_SCHEMA)


def validate_event(data: dict[str, Any]) -> None:
    """event 1行を§9 schemaで検証する。"""
    validate(instance=data, schema=EVENT_SCHEMA)


def validate_checkpoint(data: dict[str, Any]) -> None:
    """checkpointを§9 schemaで検証する。"""
    validate(instance=data, schema=CHECKPOINT_SCHEMA)


def validate_budget(data: dict[str, Any]) -> None:
    """budget snapshotの全counter/source/reasonを検証する。"""
    validate(instance=data, schema=BUDGET_SCHEMA)
