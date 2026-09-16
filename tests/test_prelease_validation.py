"""FABLE repair contracts that must fail before lease or state creation."""

from __future__ import annotations

from typing import Any

import pytest

from orc.budget_models import default_budget_caps, validate_budget_caps


@pytest.mark.parametrize("invalid", [True, 0, -1, 1.5, "1", None, []])
def test_every_numeric_cap_requires_a_positive_non_boolean_integer(invalid: Any) -> None:
    caps = default_budget_caps()
    caps["tokens_soft"] = invalid

    with pytest.raises(ValueError, match="positive integer"):
        validate_budget_caps(caps)


@pytest.mark.parametrize(
    "prefix",
    (
        "tokens",
        "child_invocations",
        "task_attempts",
        "active_seconds",
        "manager_calls",
        "result_summary",
    ),
)
def test_every_soft_cap_must_not_exceed_its_hard_cap(prefix: str) -> None:
    caps = default_budget_caps()
    soft = f"{prefix}_soft" if prefix != "result_summary" else "result_summary_soft_chars"
    hard = f"{prefix}_hard" if prefix != "result_summary" else "result_summary_hard_chars"
    caps[soft] = caps[hard] + 1

    with pytest.raises(ValueError, match="soft cap exceeds hard cap"):
        validate_budget_caps(caps)
