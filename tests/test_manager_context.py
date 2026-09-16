"""M5 pinned and variable Manager context acceptance tests."""

import json

import pytest
from jsonschema import ValidationError

from orc.manager_context import (
    ContextLimitError,
    ManagerContextBuilder,
    PinnedContextTooLarge,
    ValidatedVariable,
)
from orc.usage import BudgetSource, UsageValue
from tests.m5_helpers import pinned_context, valid_plan
from tests.m6_helpers import FixtureClearanceRegistry, fixture_clearance


def character_estimator(value: str) -> UsageValue:
    """Deterministic injectable estimator for boundary tests."""
    return UsageValue(len(value), BudgetSource.COUNT_PROXY, "test_characters")


def _plan_variable(
    plan, sequence: int, registry: FixtureClearanceRegistry
):  # type: ignore[no-untyped-def]
    clearance, registry = fixture_clearance("plan", plan, registry)
    return ValidatedVariable.from_plan(
        plan,
        sequence=sequence,
        clearance=clearance,
        clearance_registry=registry,
    )


def _builder() -> tuple[ManagerContextBuilder, FixtureClearanceRegistry]:
    registry = FixtureClearanceRegistry()
    return ManagerContextBuilder(registry, estimator=character_estimator), registry


def test_pinned_bytes_survive_variable_truncation_exactly() -> None:
    """Dropping variable context cannot mutate or truncate pinned bytes."""
    # Arrange
    builder, registry = _builder()
    pinned = pinned_context()
    variables = tuple(
        _plan_variable(valid_plan(), sequence, registry)
        for sequence in range(3)
    )
    pinned_size = character_estimator(builder.serialize_pinned(pinned).decode()).tokens

    # Act
    bundle = builder.build(
        pinned,
        variables,
        manager_input_tokens_hard=pinned_size + 450,
        model_window_tokens=10_000,
    )

    # Assert
    assert bundle.pinned_bytes == builder.serialize_pinned(pinned)
    assert bundle.dropped_count >= 1
    assert len(bundle.dropped_digests) == bundle.dropped_count


def test_pinned_only_over_cap_is_not_truncated() -> None:
    """Pinned-only overflow fails loudly instead of calling an LLM."""
    builder, _ = _builder()

    with pytest.raises(PinnedContextTooLarge, match="goal_too_large"):
        builder.build(
            pinned_context(goal="x" * 500),
            (),
            manager_input_tokens_hard=20,
            model_window_tokens=10_000,
        )


def test_newest_variable_items_are_kept_deterministically() -> None:
    """Old variable items are dropped before newer validated information."""
    # Arrange
    builder, registry = _builder()
    pinned = pinned_context()
    variables = tuple(
        _plan_variable(valid_plan(planner_size=size), sequence, registry)
        for sequence, size in enumerate(("S", "M", "L"), start=1)
    )
    newest_only = builder.build(
        pinned,
        (variables[-1],),
        manager_input_tokens_hard=10_000,
        model_window_tokens=100_000,
    )

    # Act
    result = builder.build(
        pinned,
        variables,
        manager_input_tokens_hard=newest_only.estimate.tokens,
        model_window_tokens=100_000,
    )

    # Assert
    assert result.kept_sequences == (3,)
    assert result.dropped_count == 2


def test_duplicate_variable_occurrences_have_exact_drop_count() -> None:
    """Repeated immutable artifacts are counted by occurrence, not object identity."""
    builder, registry = _builder()
    pinned = pinned_context()
    variable = _plan_variable(valid_plan(), 1, registry)
    one_item = builder.build(pinned, (variable,), 10_000, 100_000)

    result = builder.build(
        pinned,
        (variable, variable),
        one_item.estimate.tokens,
        100_000,
    )

    assert result.kept_sequences == (1,)
    assert result.dropped_count == 1


def test_raw_logs_and_invalid_json_are_not_accepted_input_types() -> None:
    """Manager context only accepts constructor-validated artifact wrappers."""
    builder, registry = _builder()

    with pytest.raises(TypeError, match="validated variable"):
        builder.build(
            pinned_context(),
            ("RAW_STDOUT",),  # type: ignore[arg-type]
            manager_input_tokens_hard=10_000,
            model_window_tokens=100_000,
        )
    with pytest.raises(ValidationError):
        _plan_variable(json.loads('{"invalid": true}'), 1, registry)
    with pytest.raises(TypeError):
        ValidatedVariable.from_plan(valid_plan(), sequence=1)  # type: ignore[call-arg]


def test_unbounded_plan_field_is_rejected_before_context_input() -> None:
    """Schema-valid variable fields are also size-bounded, not merely typed."""
    oversized = valid_plan()
    oversized["tasks"][0]["objective"] = "x" * 4001

    with pytest.raises(ValidationError):
        _plan_variable(oversized, 1, FixtureClearanceRegistry())


def test_builder_rechecks_clearance_when_private_constructor_is_called() -> None:
    """Calling the internal constructor cannot bypass the Manager-owned registry."""
    builder, _ = _builder()
    plan = valid_plan()
    foreign = FixtureClearanceRegistry()
    clearance, _ = fixture_clearance("plan", plan, foreign)
    bypassed = ValidatedVariable._create("plan", 1, plan, clearance)

    with pytest.raises(ValueError, match="unregistered"):
        builder.build(pinned_context(), (bypassed,), 10_000, 100_000)


def test_same_input_has_same_prompt_and_digest() -> None:
    """Canonical serialization is byte-for-byte deterministic."""
    builder, registry = _builder()
    variables = (_plan_variable(valid_plan(), 1, registry),)

    first = builder.build(pinned_context(), variables, 10_000, 100_000)
    second = builder.build(pinned_context(), variables, 10_000, 100_000)

    assert first.prompt_bytes == second.prompt_bytes
    assert first.prompt_digest == second.prompt_digest


def test_unknown_model_window_fails_loudly() -> None:
    """The initial-bundle 20 percent constraint is never guessed."""
    builder, _ = _builder()

    with pytest.raises(ContextLimitError, match="model_window_unknown"):
        builder.build(pinned_context(), (), 10_000, None)


def test_initial_bundle_must_fit_twenty_percent_of_model_window() -> None:
    """Known model windows enforce the child initial-bundle ratio."""
    builder, _ = _builder()

    with pytest.raises(ContextLimitError, match="initial_bundle_too_large"):
        builder.build(pinned_context(), (), 10_000, 100)


def test_planner_role_contract_states_scope_and_estimate_rules() -> None:
    """Plannerに write専用path_scope・glob禁止・read_scope・控えめ見積りを明示する。"""
    contract = pinned_context().role_contract

    assert "path_scope" in contract
    assert "read_scope" in contract
    assert "glob" in contract
    assert "estimated_invocations" in contract
