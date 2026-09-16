"""Canonical-to-Codex transport schema boundary tests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import ValidationError

from orc.attempt_staging import AttemptStaging
from orc.codex_transport_schema import (
    CODEX_SCHEMA_KEYWORDS,
    CodexTransportSchemaError,
    to_codex_transport_schema,
)
from orc.plan_schema import PLAN_SCHEMA, validate_plan
from orc.planner_staging import PlannerStaging
from orc.result_schema import RESULT_SCHEMA, validate_result
from orc.review_schema import REVIEW_SCHEMA
from orc.review_staging import ReviewStaging
from tests.m5_helpers import valid_plan
from tests.test_result_schema import valid_result


def _schema_keywords(schema: dict[str, Any]) -> set[str]:
    found: set[str] = set()

    def visit(node: dict[str, Any]) -> None:
        for keyword, value in node.items():
            found.add(keyword)
            if keyword in {"properties", "$defs"}:
                for child in value.values():
                    visit(child)
            elif keyword == "items":
                visit(value)
            elif keyword == "anyOf":
                for child in value:
                    visit(child)

    visit(schema)
    return found


@pytest.mark.parametrize("canonical", [PLAN_SCHEMA, RESULT_SCHEMA, REVIEW_SCHEMA])
def test_transport_schema_uses_only_allowlisted_keywords_without_mutation(
    canonical: dict[str, Any],
) -> None:
    before = deepcopy(canonical)

    transport = to_codex_transport_schema(canonical)

    assert _schema_keywords(transport) <= CODEX_SCHEMA_KEYWORDS
    assert canonical == before
    assert transport is not canonical


def test_transport_schema_rejects_unknown_keyword_instead_of_forwarding_it() -> None:
    canonical = {"type": "object", "properties": {}, "required": [], "mystery": True}

    with pytest.raises(CodexTransportSchemaError, match="mystery"):
        to_codex_transport_schema(canonical)


def test_canonical_unique_items_remain_and_reject_duplicate_arrays() -> None:
    assert PLAN_SCHEMA["properties"]["tasks"]["items"]["properties"]["depends_on"][
        "uniqueItems"
    ] is True
    assert RESULT_SCHEMA["properties"]["changed_files"]["uniqueItems"] is True
    assert RESULT_SCHEMA["properties"]["references"]["uniqueItems"] is True

    duplicated_plan = valid_plan()
    duplicated_plan["tasks"][0]["depends_on"] = ["task-2", "task-2"]
    duplicated_plan["tasks"].append(
        {
            **deepcopy(duplicated_plan["tasks"][0]),
            "task_id": "task-2",
            "depends_on": [],
        }
    )
    with pytest.raises(ValidationError):
        validate_plan(duplicated_plan)

    for field in ("changed_files", "references"):
        duplicated_result = valid_result()
        duplicated_result[field] = ["src/change.py", "src/change.py"]
        with pytest.raises(ValidationError):
            validate_result(
                duplicated_result,
                expected_task_id="task-1",
                expected_role="implementer",
                expected_attempt=1,
            )


@pytest.mark.parametrize(
    ("factory", "canonical"),
    [
        (lambda root: PlannerStaging(root, 1), PLAN_SCHEMA),
        (lambda root: AttemptStaging(root, 1), RESULT_SCHEMA),
        (lambda root: ReviewStaging(root, 1), REVIEW_SCHEMA),
    ],
)
def test_all_staging_paths_write_transport_schema(
    tmp_path: Path,
    factory: Any,
    canonical: dict[str, Any],
) -> None:
    staging = factory(tmp_path)
    try:
        staging.create()
        written = json.loads(staging.schema_path.read_text(encoding="utf-8"))
        assert written == to_codex_transport_schema(canonical)
        assert "uniqueItems" not in _schema_keywords(written)
    finally:
        staging.cleanup()
